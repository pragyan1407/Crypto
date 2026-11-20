"""Kerma node: TCP server, handshake, peer discovery, object exchange and block validation."""

import asyncio
import logging
import random
import time

from objects import object_id
from objectstore import ObjectStore
from peerstore import PeerStore
from protocol import (
    IGNORED_TYPES,
    INVALID_ANCESTRY,
    INVALID_FORMAT,
    INVALID_HANDSHAKE,
    MAX_PEERS_PER_MESSAGE,
    NON_FATAL_ERRORS,
    UNFINDABLE_OBJECT,
    UNKNOWN_OBJECT,
    ProtocolError,
    canonical_json,
    make_error,
    make_getobject,
    make_getpeers,
    make_hello,
    make_ihaveobject,
    make_object,
    make_peers,
    parse_json_object,
    validate_getobject,
    validate_getpeers,
    validate_hello,
    validate_ihaveobject,
    validate_object_message,
    validate_peers,
)
from validation import validate_block_body, validate_block_header, validate_object

log = logging.getLogger("kerma.node")

DEFAULT_PORT = 18018
DEFAULT_BOOTSTRAP = "128.130.122.101:18018"
DEFAULT_AGENT = "Drachma 0.1 (Python)"

HELLO_TIMEOUT = 20.0  # seconds until a hello must have arrived
CONNECT_TIMEOUT = 5.0
WRITE_TIMEOUT = 10.0
MAX_LINE = 1 << 20  # longest accepted message, in bytes
MAX_WRITE_BUFFER = 4 << 20  # a peer that lets this much pile up unread is dropped
FETCH_TIMEOUT = 5.0  # seconds to wait for the transactions of a block before giving up


class _PendingBlock:
    """A block whose validation waits for transactions that are being fetched."""

    def __init__(self, missing):
        self.missing = set(missing)
        self.future = asyncio.get_running_loop().create_future()  # result: all arrived / exception: ancestor invalid


class Node:
    def __init__(
        self,
        *,
        data_dir,
        host="0.0.0.0",
        port=DEFAULT_PORT,
        advertise=None,
        bootstrap=(DEFAULT_BOOTSTRAP,),
        agent=DEFAULT_AGENT,
        max_outbound=5,
        max_connections=100,
        connect_interval=10.0,
        retry_after=60.0,
        reseed_interval=300.0,
        hello_timeout=HELLO_TIMEOUT,
        max_line=MAX_LINE,
        fetch_timeout=FETCH_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.advertise = advertise or None
        self.bootstrap = tuple(bootstrap)
        self.agent = agent
        self.max_outbound = max_outbound
        self.max_connections = max_connections
        self.connect_interval = connect_interval
        self.retry_after = retry_after
        self.reseed_interval = reseed_interval
        self.hello_timeout = hello_timeout
        self.max_line = max_line
        self.fetch_timeout = fetch_timeout

        self.store = PeerStore(f"{data_dir}/peers.json")
        self.objects = ObjectStore(f"{data_dir}/objects.db")
        self._server = None
        self._tasks = set()
        self._writers = set()
        self._ready = set()  # writers of connections whose handshake is complete (gossip targets)
        self._outbound = set()  # addresses we are currently dialing / connected to
        self._outbound_addr = {}  # writer -> address, for connections we dialed ourselves
        self._closing = set()  # writers we declared faulty and are about to close
        self._waiting = {}  # txid -> set of _PendingBlock waiting for that transaction
        self._pending_ids = set()  # ids of blocks that are waiting for transactions
        self._block_tasks = {}  # task completing a pending block -> writer of the peer that sent it
        self._last_reseed = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._server = await asyncio.start_server(
            self._on_inbound, self.host, self.port, limit=self.max_line
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self._seed_bootstrap()
        self._spawn(self._connect_loop())
        log.info("listening on %s:%d", self.host, self.port)

    async def stop(self):
        if self._server is not None:
            self._server.close()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        for writer in list(self._writers):
            writer.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self.objects.close()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Peer discovery: which peers to dial
    # ------------------------------------------------------------------

    def _seed_bootstrap(self):
        for addr in self.bootstrap:
            if addr != self.advertise:
                self.store.add(addr, trusted=True, save=False)
        self.store.save()
        self._last_reseed = asyncio.get_running_loop().time()

    async def _connect_loop(self):
        while True:
            try:
                self._fill_outbound()
            except Exception:
                log.exception("error while choosing peers to connect to")
            await asyncio.sleep(self.connect_interval)

    def _fill_outbound(self):
        """Policy: keep up to `max_outbound` connections to random known peers.

        A peer is retried at most once per `retry_after` seconds. If we have
        forgotten every peer, the bootstrap peers are re-added (rate limited).
        """
        loop_now = asyncio.get_running_loop().time()
        if len(self.store) == 0 and self.bootstrap and loop_now - self._last_reseed >= self.reseed_interval:
            self._seed_bootstrap()

        need = self.max_outbound - len(self._outbound)
        if need <= 0:
            return
        exclude = set(self._outbound)
        if self.advertise:
            exclude.add(self.advertise)
        candidates = self.store.candidates(exclude, time.time(), self.retry_after)
        random.shuffle(candidates)
        for addr in candidates[:need]:
            self._outbound.add(addr)
            self.store.record_attempt(addr)
            self._spawn(self._connect(addr))

    async def _connect(self, addr):
        host, _, port = addr.rpartition(":")
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, int(port), limit=self.max_line),
                    CONNECT_TIMEOUT,
                )
            except (OSError, asyncio.TimeoutError):
                if self.store.record_failure(addr):
                    log.info("forgot unreachable peer %s", addr)
                return
            await self._serve(reader, writer, addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("outbound connection to %s failed unexpectedly", addr)
        finally:
            self._outbound.discard(addr)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    async def _on_inbound(self, reader, writer):
        if len(self._writers) >= self.max_connections:
            writer.close()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            await self._serve(reader, writer, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbound connection failed unexpectedly")

    async def _serve(self, reader, writer, outbound_addr):
        """Run one connection (inbound if outbound_addr is None) until it ends."""
        peername = writer.get_extra_info("peername")
        label = outbound_addr or (f"{peername[0]}:{peername[1]}" if peername else "unknown")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.hello_timeout
        handshaken = False
        faulty = False
        self._writers.add(writer)
        if outbound_addr:
            self._outbound_addr[writer] = outbound_addr
        log.info("connection with %s opened", label)
        try:
            await self._send(writer, make_hello(self.agent))
            await self._send(writer, make_getpeers())
            while True:
                try:
                    if handshaken:
                        line = await reader.readuntil(b"\n")
                    else:
                        remaining = max(0.0, deadline - loop.time())
                        line = await asyncio.wait_for(reader.readuntil(b"\n"), remaining)
                except asyncio.TimeoutError:
                    raise ProtocolError(INVALID_HANDSHAKE, "no hello received within the time limit") from None
                except asyncio.IncompleteReadError:
                    return  # peer closed the connection
                except (asyncio.LimitOverrunError, ValueError):
                    raise ProtocolError(INVALID_FORMAT, "message too long") from None
                if writer in self._closing:
                    continue  # we already declared this peer faulty: just wait for it to go away
                try:
                    handshaken = await self._process(line, handshaken, writer, outbound_addr)
                except ProtocolError as err:
                    if err.name not in NON_FATAL_ERRORS:
                        raise
                    await self._send_error(writer, err)  # e.g. UNKNOWN_OBJECT: peer is not faulty
        except ProtocolError as err:
            faulty = True
            log.warning("peer %s is faulty: %s", label, err)
            if outbound_addr:
                self.store.forget(outbound_addr)
            await self._send_error(writer, err)
            # Blocks of this peer that were waiting for an object that just turned out to be invalid
            # report INVALID_ANCESTRY now, before the connection is half-closed.
            await self._settle_block_tasks(writer)
            await self._linger(reader, writer)
        except (OSError, asyncio.TimeoutError):
            pass  # network problem, nothing to do
        finally:
            self._writers.discard(writer)
            self._ready.discard(writer)
            self._closing.discard(writer)
            self._outbound_addr.pop(writer, None)
            for task, source in list(self._block_tasks.items()):
                if source is writer:
                    task.cancel()  # nobody left to fetch the missing transactions from
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("connection with %s closed%s", label, " (faulty)" if faulty else "")

    async def _process(self, line, handshaken, writer, outbound_addr):
        """Handle one received line. Returns whether the handshake is complete."""
        msg = parse_json_object(line)
        mtype = msg["type"]

        if mtype == "hello":
            validate_hello(msg)
            if handshaken:
                raise ProtocolError(INVALID_HANDSHAKE, "hello received after handshake")
            if outbound_addr:
                self.store.mark_success(outbound_addr)
            self._ready.add(writer)
            return True

        if not handshaken:
            raise ProtocolError(INVALID_HANDSHAKE, "first message must be hello")

        if mtype == "getpeers":
            validate_getpeers(msg)
            await self._send(writer, self._peers_message())
        elif mtype == "peers":
            peers = validate_peers(msg)
            self.store.add_many(p for p in peers if p != self.advertise)
        elif mtype == "getobject":
            objectid = validate_getobject(msg)
            obj = self.objects.get(objectid)
            if obj is None:
                raise ProtocolError(UNKNOWN_OBJECT, "object is not known")
            await self._send(writer, make_object(obj))
        elif mtype == "ihaveobject":
            objectid = validate_ihaveobject(msg)
            if not self.objects.has(objectid):
                await self._send(writer, make_getobject(objectid))
        elif mtype == "object":
            self._handle_object(validate_object_message(msg), writer)
        elif mtype in IGNORED_TYPES:
            pass  # valid message type, handled in later tasks
        else:
            raise ProtocolError(INVALID_FORMAT, "unknown message type")
        return True

    def _handle_object(self, obj, source):
        """Validate, store and gossip an object received from `source`."""
        try:
            objectid = object_id(obj)
        except (ValueError, RecursionError):
            raise ProtocolError(INVALID_FORMAT, "object cannot be encoded as JSON") from None
        if self.objects.has(objectid):
            return  # already known: ignore
        if obj.get("type") == "block":
            self._handle_block(obj, objectid, source)
            return
        try:
            validate_object(obj, self.objects)  # raises ProtocolError if invalid
        except ProtocolError as err:
            if err.name not in NON_FATAL_ERRORS:
                self._fail_waiters(objectid, err)  # blocks that wait for this transaction are invalid, too
            raise
        if self.objects.put(objectid, obj):
            log.info("stored new object %s", objectid)
            self._broadcast(make_ihaveobject(objectid), exclude=source)
            self._object_arrived(objectid)

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------

    def _handle_block(self, block, blockid, source):
        """Validate a block received from `source`; fetch its missing transactions first if needed."""
        if blockid in self._pending_ids:
            return  # already waiting for its transactions
        missing = validate_block_header(block, self.objects)  # raises ProtocolError if invalid
        if not missing:
            self._finish_block(block, blockid, source)
            return

        pending = _PendingBlock(missing)
        for txid in missing:
            self._waiting.setdefault(txid, set()).add(pending)
        self._pending_ids.add(blockid)
        task = self._spawn(self._complete_block(block, blockid, source, pending, missing))
        self._block_tasks[task] = source
        task.add_done_callback(lambda t: self._block_tasks.pop(t, None))

    def _finish_block(self, block, blockid, source):
        """All transactions are available: execute them, store the block and gossip it."""
        height, utxo = validate_block_body(block, self.objects)  # raises ProtocolError if invalid
        if self.objects.put_block(blockid, block, height, utxo):
            log.info("stored new block %s at height %d", blockid, height)
            self._broadcast(make_ihaveobject(blockid), exclude=source)

    async def _complete_block(self, block, blockid, source, pending, missing):
        """Ask `source` for the missing transactions, wait for them (at most fetch_timeout) and finish."""
        try:
            for txid in missing:
                self._write_nowait(source, make_getobject(txid))
            try:
                await asyncio.wait_for(pending.future, self.fetch_timeout)
            except asyncio.TimeoutError:
                raise ProtocolError(
                    UNFINDABLE_OBJECT, f"could not fetch the transactions of block {blockid} in time"
                ) from None
            self._finish_block(block, blockid, source)
        except ProtocolError as err:
            if err.name in NON_FATAL_ERRORS:  # UNFINDABLE_OBJECT / INVALID_ANCESTRY: the peer may be fine
                await self._send_error(source, err)
            else:
                log.warning("block %s is invalid: %s", blockid, err)
                await self._fail_connection(source, err)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unexpected error while validating block %s", blockid)
        finally:
            self._pending_ids.discard(blockid)
            for txid in missing:
                waiting = self._waiting.get(txid)
                if waiting is not None:
                    waiting.discard(pending)
                    if not waiting:
                        del self._waiting[txid]

    def _object_arrived(self, objectid):
        """A new object was stored: resume the blocks that were waiting for it."""
        for pending in self._waiting.pop(objectid, ()):
            pending.missing.discard(objectid)
            if not pending.missing and not pending.future.done():
                pending.future.set_result(None)

    def _fail_waiters(self, objectid, err):
        """An object was found to be invalid: the blocks that were waiting for it are invalid, too."""
        for pending in self._waiting.pop(objectid, ()):
            if not pending.future.done():
                pending.future.set_exception(
                    ProtocolError(INVALID_ANCESTRY, f"object {objectid} is invalid ({err.name})")
                )

    async def _settle_block_tasks(self, writer, timeout=0.2):
        """Give the pending blocks of this connection a moment to report their outcome."""
        tasks = [task for task, source in self._block_tasks.items() if source is writer]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def _fail_connection(self, writer, err):
        """Report a fatal error found outside the connection's own read loop, then disconnect.

        Half-close first and close later, so that the peer can read the error before the connection ends.
        """
        await self._send_error(writer, err)
        self._closing.add(writer)
        self._ready.discard(writer)
        address = self._outbound_addr.get(writer)
        if address:
            self.store.forget(address)
        try:
            if not writer.is_closing() and writer.can_write_eof():
                writer.write_eof()
        except Exception:  # noqa: BLE001 - the connection may already be gone
            pass
        asyncio.get_running_loop().call_later(1.0, writer.close)

    def _broadcast(self, obj, exclude=None):
        """Send a message to every connected peer that completed the handshake.

        The peer we just got the news from is skipped, it obviously knows already.
        """
        data = canonical_json(obj) + b"\n"
        for writer in list(self._ready):
            if writer is exclude:
                continue
            try:
                if writer.transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
                    writer.close()  # peer is not reading: drop it instead of buffering forever
                    continue
                writer.write(data)
            except Exception:  # noqa: BLE001 - one broken connection must not stop the broadcast
                writer.close()

    def _peers_message(self):
        """Our own address (if configured) first, then the best known peers, at most 30."""
        peers = []
        if self.advertise:
            peers.append(self.advertise)
        for addr in self.store.select_for_sharing(MAX_PEERS_PER_MESSAGE):
            if len(peers) >= MAX_PEERS_PER_MESSAGE:
                break
            if addr != self.advertise:
                peers.append(addr)
        return make_peers(peers)

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    async def _send(self, writer, obj):
        writer.write(canonical_json(obj) + b"\n")
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)

    def _write_nowait(self, writer, obj):
        if not writer.is_closing():
            writer.write(canonical_json(obj) + b"\n")

    async def _send_error(self, writer, err):
        try:
            await self._send(writer, make_error(err.name, err.msg))
        except (OSError, asyncio.TimeoutError):
            pass

    async def _linger(self, reader, writer):
        """Half-close, then briefly drain what the peer still sends.

        Closing a socket that has unread data makes TCP reset the connection,
        which can destroy the error message we just sent before the peer reads it.
        """
        try:
            if writer.can_write_eof():
                writer.write_eof()
            loop = asyncio.get_running_loop()
            end = loop.time() + 1.0
            total = 0
            while total < 4 * 65536:
                remaining = end - loop.time()
                if remaining <= 0:
                    break
                data = await asyncio.wait_for(reader.read(65536), remaining)
                if not data:
                    break
                total += len(data)
        except (OSError, asyncio.TimeoutError):
            pass
