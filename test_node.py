"""Integration tests that mirror the Task 1 grader cases.

Each test starts a real node on a random local port and talks to it over TCP.
"""

import asyncio
import json
import socket
import tempfile
import unittest

from fixtures import PDF_COINBASE, PDF_COINBASE_ID, PDF_TX, PDF_TX_ID
from node import Node
from objects import GENESIS, GENESIS_ID, object_id
from protocol import canonical_json
from txtools import make_coinbase, make_tx, new_private_key, pubkey_hex, txid

HELLO = {"type": "hello", "version": "0.10.0", "agent": "Test Grader"}
PEERS = ["kermanode.net:18018", "138.197.191.170:18018", "example.org:18019"]


class Client:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer)

    async def send(self, obj):
        await self.send_raw(canonical_json(obj) + b"\n")

    async def send_raw(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self, timeout=3.0):
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        return json.loads(line) if line else None

    async def handshake(self):
        """Read the node's hello + getpeers, then send our hello."""
        hello = await self.recv()
        getpeers = await self.recv()
        await self.send(HELLO)
        return hello, getpeers

    async def expect_closed(self, timeout=3.0):
        data = await asyncio.wait_for(self.reader.read(), timeout)
        return data == b""

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


class NodeTestCase(unittest.IsolatedAsyncioTestCase):
    hello_timeout = 20.0

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = await self.start_node()
        self.addAsyncCleanup(self._stop_node)

    async def _stop_node(self):
        await self.node.stop()

    async def start_node(self, **kwargs):
        options = dict(
            data_dir=self.tmp.name,
            host="127.0.0.1",
            port=0,
            bootstrap=(),
            hello_timeout=self.hello_timeout,
        )
        options.update(kwargs)
        node = Node(**options)
        await node.start()
        return node

    async def client(self, node=None):
        c = await Client.connect((node or self.node).port)
        self.addAsyncCleanup(c.close)
        return c

    async def ready_client(self):
        c = await self.client()
        await c.handshake()
        return c

    async def assertError(self, client, name):
        msg = await client.recv()
        self.assertEqual(msg["type"], "error")
        self.assertEqual(msg["name"], name)
        self.assertIsInstance(msg["msg"], str)
        self.assertTrue(await client.expect_closed())


class HandshakeTest(NodeTestCase):
    async def test_hello_then_getpeers_on_connect(self):
        c = await self.client()
        hello, getpeers = await c.handshake()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["version"], "0.10.0")
        self.assertEqual(set(hello), {"type", "version", "agent"})
        self.assertEqual(getpeers, {"type": "getpeers"})

    async def test_reconnect(self):
        c = await self.ready_client()
        await c.close()
        c2 = await self.client()
        hello, _ = await c2.handshake()
        self.assertEqual(hello["type"], "hello")

    async def test_two_simultaneous_connections(self):
        a = await self.ready_client()
        b = await self.ready_client()
        for c in (a, b):
            await c.send({"type": "getpeers"})
        for c in (a, b):
            self.assertEqual((await c.recv())["type"], "peers")

    async def test_message_before_hello(self):
        for message in [{"type": "getpeers"}, {"type": "peers", "peers": []}, {"type": "getchaintip"}]:
            with self.subTest(message=message):
                c = await self.client()
                await c.recv()  # hello
                await c.recv()  # getpeers
                await c.send(message)
                await self.assertError(c, "INVALID_HANDSHAKE")

    async def test_second_hello(self):
        c = await self.ready_client()
        await c.send(HELLO)
        await self.assertError(c, "INVALID_HANDSHAKE")

    async def test_wait_for_hello_times_out(self):
        await self.node.stop()
        self.node = await self.start_node(hello_timeout=0.5)
        c = await self.client()
        await c.recv()
        await c.recv()
        await self.assertError(c, "INVALID_HANDSHAKE")

    async def test_hello_arrives_in_pieces(self):
        c = await self.client()
        await c.recv()
        await c.recv()
        data = canonical_json(HELLO) + b"\n"
        await c.send_raw(data[:10])
        await asyncio.sleep(0.1)
        await c.send_raw(data[10:])
        await c.send({"type": "getpeers"})
        self.assertEqual((await c.recv())["type"], "peers")


class InvalidMessageTest(NodeTestCase):
    INVALID = [
        b"Wbgygvf7rgtyv7tfbgy{{{\n",
        b'"type":"diufygeuybhv"\n',
        b'{"type":"diufygeuybhv"}\n',
        b"[]\n",
        b"\n",
        b'{"type":"getpeers","extra":1}\n',
    ]
    INVALID_HELLO = [
        b'{"type":"hello"}\n',
        b'{"type":"hello","version":"jd3.x"}\n',
        b'{"type":"hello","version":"5.8.2"}\n',
        b'{"type":"hello","version":"5.8.2","agent":"x"}\n',
        b'{"type":"hello","version":"0.10.0","agent":"x","extra":1}\n',
    ]

    async def test_invalid_message_after_handshake(self):
        for line in self.INVALID:
            with self.subTest(line=line):
                c = await self.ready_client()
                await c.send_raw(line)
                await self.assertError(c, "INVALID_FORMAT")

    async def test_invalid_message_as_first_message(self):
        for line in self.INVALID[:1] + self.INVALID_HELLO:
            with self.subTest(line=line):
                c = await self.client()
                await c.recv()
                await c.recv()
                await c.send_raw(line)
                await self.assertError(c, "INVALID_FORMAT")

    async def test_invalid_peers_entry(self):
        for bad in ["nodotindomain:1234", "256.2.3.4:18018", "192.168.0.2:18018", "kermanode.net", "1.2.3.4:2000000"]:
            with self.subTest(peer=bad):
                c = await self.ready_client()
                await c.send({"type": "peers", "peers": ["kermanode.net:18018", bad]})
                await self.assertError(c, "INVALID_FORMAT")
        self.assertNotIn("kermanode.net:18018", self.node.store)

    async def test_too_many_peers(self):
        c = await self.ready_client()
        await c.send({"type": "peers", "peers": ["kermanode.net:18018"] * 31})
        await self.assertError(c, "INVALID_FORMAT")

    async def test_overlong_message(self):
        await self.node.stop()
        self.node = await self.start_node(max_line=1024)
        c = await self.ready_client()
        await c.send_raw(b"x" * 4000)
        await self.assertError(c, "INVALID_FORMAT")

    async def test_known_but_unhandled_types_are_ignored(self):
        c = await self.ready_client()
        for message in [
            {"type": "getchaintip"},
            {"type": "getmempool"},
            {"type": "mempool", "txids": []},
            {"type": "error", "name": "UNKNOWN_OBJECT", "msg": "x"},
        ]:
            await c.send(message)
        await c.send({"type": "getpeers"})
        self.assertEqual((await c.recv())["type"], "peers")  # still connected and responsive


class PeersTest(NodeTestCase):
    async def test_getpeers_returns_valid_peers_message(self):
        c = await self.ready_client()
        await c.send({"type": "getpeers"})
        msg = await c.recv()
        self.assertEqual(msg, {"type": "peers", "peers": []})

    async def test_getpeers_split_across_packets(self):
        c = await self.ready_client()
        await c.send_raw(b'{"type":"ge')
        await asyncio.sleep(0.1)
        await c.send_raw(b'tpeers"}\n')
        self.assertEqual((await c.recv())["type"], "peers")

    async def test_multiple_messages_in_one_packet(self):
        c = await self.ready_client()
        await c.send_raw(b'{"type":"getpeers"}\n{ "type" : "getpeers" }\n')
        self.assertEqual((await c.recv())["type"], "peers")
        self.assertEqual((await c.recv())["type"], "peers")

    async def test_received_peers_are_returned_after_reconnect(self):
        c = await self.ready_client()
        await c.send({"type": "peers", "peers": PEERS})
        await c.close()

        c2 = await self.ready_client()
        await c2.send({"type": "getpeers"})
        msg = await c2.recv()
        self.assertEqual(msg["type"], "peers")
        self.assertTrue(set(PEERS) <= set(msg["peers"]))

    async def test_peers_survive_restart(self):
        c = await self.ready_client()
        await c.send({"type": "peers", "peers": PEERS})
        await c.send({"type": "getpeers"})
        await c.recv()  # once answered, the peers message has been processed
        await self.node.stop()

        self.node = await self.start_node()
        c2 = await self.ready_client()
        await c2.send({"type": "getpeers"})
        msg = await c2.recv()
        self.assertEqual(set(msg["peers"]), set(PEERS))

    async def test_at_most_30_peers_and_own_address_first(self):
        await self.node.stop()
        self.node = await self.start_node(advertise="mynode.example.org:18018")
        c = await self.ready_client()
        many = [f"node{i}.example.org:18018" for i in range(30)]
        await c.send({"type": "peers", "peers": many})
        await c.send({"type": "peers", "peers": [f"other{i}.example.org:18018" for i in range(20)]})
        await c.send({"type": "getpeers"})
        msg = await c.recv()
        self.assertEqual(len(msg["peers"]), 30)
        self.assertEqual(msg["peers"][0], "mynode.example.org:18018")
        self.assertEqual(len(set(msg["peers"])), 30)


class OutboundTest(unittest.IsolatedAsyncioTestCase):
    """The node dialing other peers (bootstrap, discovery, forgetting)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = None

    async def start_node(self, bootstrap, **kwargs):
        self.node = Node(
            data_dir=self.tmp.name,
            host="127.0.0.1",
            port=0,
            bootstrap=bootstrap,
            connect_interval=0.05,
            retry_after=0.05,
            **kwargs,
        )
        await self.node.start()
        self.addAsyncCleanup(self.node.stop)

    async def fake_peer(self, handler):
        async def wrapped(reader, writer):
            try:
                await handler(reader, writer)
            finally:
                writer.close()  # otherwise server.wait_closed() hangs on Python 3.12+

        server = await asyncio.start_server(wrapped, "127.0.0.1", 0)
        self.addAsyncCleanup(self._close_server, server)
        return f"127.0.0.1:{server.sockets[0].getsockname()[1]}"

    @staticmethod
    async def _close_server(server):
        server.close()
        await server.wait_closed()

    async def wait_until(self, predicate, timeout=5.0):
        end = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > end:
                self.fail("condition not reached in time")
            await asyncio.sleep(0.02)

    async def test_dials_bootstrap_and_does_handshake_and_peer_exchange(self):
        received = []
        done = asyncio.Event()

        async def handler(reader, writer):
            writer.write(canonical_json(HELLO) + b"\n" + b'{"type":"getpeers"}\n')
            await writer.drain()
            for _ in range(3):  # hello, getpeers, peers (reply to our getpeers)
                line = await reader.readline()
                if not line:
                    break
                received.append(json.loads(line))
                if len(received) == 2:
                    writer.write(canonical_json({"type": "peers", "peers": PEERS}) + b"\n")
                    await writer.drain()
            done.set()

        addr = await self.fake_peer(handler)
        await self.start_node((addr,))
        await asyncio.wait_for(done.wait(), 5)

        self.assertEqual([m["type"] for m in received[:3]], ["hello", "getpeers", "peers"])
        await self.wait_until(lambda: set(PEERS) <= set(self.node.store.addresses()))
        self.assertIn(addr, self.node.store)  # handshake succeeded, so the peer is kept

    async def test_faulty_peer_is_forgotten(self):
        async def handler(reader, writer):
            writer.write(b"this is not json\n")
            await writer.drain()
            await reader.read()

        addr = await self.fake_peer(handler)
        await self.start_node((addr,))
        await self.wait_until(lambda: addr not in self.node.store)

    async def test_unreachable_peer_is_forgotten(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = f"127.0.0.1:{s.getsockname()[1]}"
        await self.start_node((dead,))
        self.assertIn(dead, self.node.store)
        await self.wait_until(lambda: dead not in self.node.store)

    async def test_does_not_connect_to_more_than_max_outbound(self):
        connections = []

        async def handler(reader, writer):
            connections.append(writer)
            await reader.read()

        addrs = [await self.fake_peer(handler) for _ in range(4)]
        await self.start_node(tuple(addrs), max_outbound=2)
        await self.wait_until(lambda: len(connections) >= 2)
        await asyncio.sleep(0.3)
        self.assertEqual(len(connections), 2)


class GraderScenarioTest(NodeTestCase):
    """The exact messages the course grader sends (taken from a real grader log)."""

    GRADER_INVALID_MESSAGES = [
        b"hgjhlkjhlkj{{\n",
        b'{"typ":"diufygeuybhv"}\n',
        b'{"type":"hello"}\n',
        b'{"type":"hello", "version":"jd3.x"}\n',
        b'{"type":"hello", "version":"0.8.0"}\n',
    ]

    async def assertErrorOneOf(self, client, names):
        msg = await client.recv()
        self.assertEqual(msg["type"], "error")
        self.assertIn(msg["name"], names)
        self.assertTrue(await client.expect_closed())

    async def test_invalid_first_messages(self):
        for line in self.GRADER_INVALID_MESSAGES:
            with self.subTest(line=line):
                c = await self.client()
                await c.recv()  # hello
                await c.recv()  # getpeers
                await c.send_raw(line)
                await self.assertErrorOneOf(c, {"INVALID_FORMAT", "INVALID_HANDSHAKE"})

    async def test_fragmented_getpeers_after_hello(self):
        c = await self.client()
        await c.recv()
        await c.recv()
        await c.send(HELLO)
        await c.send_raw(b'{"type":"g')
        await asyncio.sleep(0.1)
        await c.send_raw(b'etpeers"}\n')
        msg = await c.recv()
        self.assertEqual(msg["type"], "peers")

    async def test_peers_sent_earlier_come_back(self):
        # grader: hello, getpeers, peers [...] in one go, then getpeers again later
        c = await self.client()
        await c.send_raw(
            canonical_json(HELLO)
            + b"\n"
            + b'{"type":"getpeers"}\n'
            + b'{"peers":["host.docker.internal:18019"],"type":"peers"}\n'
        )
        self.assertEqual((await c.recv())["type"], "hello")
        self.assertEqual((await c.recv())["type"], "getpeers")
        self.assertEqual((await c.recv())["type"], "peers")
        await asyncio.sleep(0.5)
        await c.send({"type": "getpeers"})
        msg = await c.recv()
        self.assertEqual(msg["type"], "peers")
        self.assertIn("host.docker.internal:18019", msg["peers"])

    async def test_incomplete_hello_gets_error(self):
        # grader: sends '{"type":"hello", "ver' and then waits for an error
        await self.node.stop()
        self.node = await self.start_node(hello_timeout=0.5)
        c = await self.client()
        await c.recv()
        await c.recv()
        await c.send_raw(b'{"type":"hello", "ver')
        await self.assertErrorOneOf(c, {"INVALID_FORMAT", "INVALID_HANDSHAKE"})


class ObjectExchangeTest(NodeTestCase):
    """Task 2: object exchange, gossip and transaction validation with two peers."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = new_private_key()
        self.bob = new_private_key()
        self.alice_pub = pubkey_hex(self.alice)
        self.bob_pub = pubkey_hex(self.bob)
        self.coinbase = make_coinbase(self.alice_pub, value=1000)
        self.cb_id = txid(self.coinbase)

    def spend(self, outputs=None, index=0, key=None, txid_=None):
        return make_tx(
            [(txid_ or self.cb_id, index, key or self.alice)],
            outputs if outputs is not None else [(self.bob_pub, 1000)],
        )

    async def send_object(self, client, obj):
        await client.send({"type": "object", "object": obj})

    async def sync(self, client):
        """Round trip that proves the node has processed everything we sent before."""
        await client.send({"type": "getpeers"})
        self.assertEqual((await client.recv())["type"], "peers")

    async def assertSilent(self, client, timeout=0.3):
        with self.assertRaises(asyncio.TimeoutError):
            await client.recv(timeout=timeout)

    async def two_peers(self):
        return await self.ready_client(), await self.ready_client()

    # -- exchange -------------------------------------------------------------

    async def test_sender_gets_object_back_on_request(self):
        g1 = await self.ready_client()
        await self.send_object(g1, self.coinbase)
        await g1.send({"type": "getobject", "objectid": self.cb_id})
        self.assertEqual(await g1.recv(), {"type": "object", "object": self.coinbase})

    async def test_other_peer_gets_object_on_request(self):
        g1, g2 = await self.two_peers()
        await self.send_object(g1, self.coinbase)
        await self.sync(g1)
        await g2.recv()  # gossip about the new object
        await g2.send({"type": "getobject", "objectid": self.cb_id})
        self.assertEqual(await g2.recv(), {"type": "object", "object": self.coinbase})

    async def test_other_peer_receives_ihaveobject(self):
        g1, g2 = await self.two_peers()
        await self.send_object(g1, self.coinbase)
        self.assertEqual(await g2.recv(), {"type": "ihaveobject", "objectid": self.cb_id})
        await self.sync(g1)
        await self.assertSilent(g1)  # the sender is not told about its own object

    async def test_known_object_is_not_gossiped_again(self):
        g1, g2 = await self.two_peers()
        await self.send_object(g1, self.coinbase)
        await g2.recv()
        await self.send_object(g1, self.coinbase)
        await self.send_object(g2, self.coinbase)
        await self.sync(g1)
        await self.sync(g2)
        await self.assertSilent(g2)

    async def test_ihaveobject_for_unknown_object_triggers_getobject(self):
        g1 = await self.ready_client()
        unknown = "ab" * 32
        await g1.send({"type": "ihaveobject", "objectid": unknown})
        self.assertEqual(await g1.recv(), {"type": "getobject", "objectid": unknown})

    async def test_ihaveobject_for_known_object_is_not_requested(self):
        g1 = await self.ready_client()
        await self.send_object(g1, self.coinbase)
        await g1.send({"type": "ihaveobject", "objectid": self.cb_id})
        await g1.send({"type": "ihaveobject", "objectid": GENESIS_ID})
        await self.sync(g1)  # the next message is peers, not getobject

    async def test_getobject_for_unknown_object(self):
        g1 = await self.ready_client()
        await g1.send({"type": "getobject", "objectid": "cd" * 32})
        msg = await g1.recv()
        self.assertEqual((msg["type"], msg["name"]), ("error", "UNKNOWN_OBJECT"))
        await self.sync(g1)  # connection stays open

    async def test_genesis_block_is_known(self):
        g1 = await self.ready_client()
        await g1.send({"type": "getobject", "objectid": GENESIS_ID})
        msg = await g1.recv()
        self.assertEqual(msg, {"type": "object", "object": GENESIS})
        self.assertEqual(object_id(msg["object"]), GENESIS_ID)

    async def test_pdf_example_objects(self):
        g1, g2 = await self.two_peers()
        await self.send_object(g1, PDF_COINBASE)
        await self.send_object(g1, PDF_TX)
        self.assertEqual(await g2.recv(), {"type": "ihaveobject", "objectid": PDF_COINBASE_ID})
        self.assertEqual(await g2.recv(), {"type": "ihaveobject", "objectid": PDF_TX_ID})
        await g2.send({"type": "getobject", "objectid": PDF_TX_ID})
        self.assertEqual(await g2.recv(), {"type": "object", "object": PDF_TX})

    async def test_non_canonical_json_object_is_accepted(self):
        g1 = await self.ready_client()
        body = json.dumps(self.coinbase, indent=1).replace("\n", " ")
        await g1.send_raw(('{ "object" : ' + body + ' , "type" : "object" }\n').encode())
        await g1.send({"type": "getobject", "objectid": self.cb_id})
        self.assertEqual((await g1.recv())["object"], self.coinbase)

    async def test_transaction_chain(self):
        g1, g2 = await self.two_peers()
        first = self.spend(outputs=[(self.bob_pub, 600), (self.alice_pub, 400)])
        second = make_tx([(txid(first), 0, self.bob)], [(self.alice_pub, 600)])
        for obj in (self.coinbase, first, second):
            await self.send_object(g1, obj)
        for obj in (self.coinbase, first, second):
            self.assertEqual(await g2.recv(), {"type": "ihaveobject", "objectid": txid(obj)})

    # -- validation -----------------------------------------------------------

    async def test_invalid_transactions_are_rejected_and_not_gossiped(self):
        g2 = await self.ready_client()
        setup = await self.ready_client()
        await self.send_object(setup, self.coinbase)
        await g2.recv()  # gossip about the coinbase

        double_spend = make_tx([(self.cb_id, 0, self.alice), (self.cb_id, 0, self.alice)], [(self.bob_pub, 1)])
        negative = self.spend()
        negative["outputs"][0]["value"] = -1
        cases = {
            "INVALID_TX_SIGNATURE": self.spend(key=self.bob),
            "INVALID_TX_CONSERVATION": self.spend(outputs=[(self.bob_pub, 1001)]),
            "INVALID_TX_OUTPOINT": self.spend(index=3),
            "INVALID_FORMAT": negative,
            "INVALID_TX_CONSERVATION ": double_spend,
        }
        for expected, tx in cases.items():
            with self.subTest(expected):
                g1 = await self.ready_client()
                await self.send_object(g1, tx)
                await self.assertError(g1, expected.strip())
                await self.assertSilent(g2)
                self.assertFalse(self.node.objects.has(object_id(tx)))

    async def test_unknown_ancestor_is_not_fatal_and_works_once_ancestor_arrives(self):
        g1, g2 = await self.two_peers()
        tx = self.spend()
        await self.send_object(g1, tx)
        msg = await g1.recv()
        self.assertEqual((msg["type"], msg["name"]), ("error", "UNKNOWN_OBJECT"))
        await self.sync(g1)  # not treated as faulty
        await self.assertSilent(g2)

        await self.send_object(g1, self.coinbase)
        await self.send_object(g1, tx)
        self.assertEqual((await g2.recv())["objectid"], self.cb_id)
        self.assertEqual((await g2.recv())["objectid"], txid(tx))

    async def test_unknown_object_types_are_rejected(self):
        for obj in [{"type": "foo"}, {"no": "type"}]:
            with self.subTest(obj=str(obj)[:30]):
                g1 = await self.ready_client()
                await self.send_object(g1, obj)
                await self.assertError(g1, "INVALID_FORMAT")

    async def test_fake_genesis_block_is_rejected(self):
        # Since Task 3 blocks are validated: a second block with previd null is INVALID_GENESIS.
        g1 = await self.ready_client()
        await self.send_object(g1, dict(GENESIS, nonce="00" * 32))
        await self.assertError(g1, "INVALID_GENESIS")

    async def test_resending_genesis_is_ignored(self):
        g1 = await self.ready_client()
        await self.send_object(g1, GENESIS)
        await self.sync(g1)

    async def test_malformed_object_messages(self):
        for message in [
            {"type": "object"},
            {"type": "object", "object": "x"},
            {"type": "object", "object": [1]},
            {"type": "object", "object": {}, "extra": 1},
            {"type": "getobject", "objectid": "AB" * 32},
            {"type": "getobject"},
            {"type": "ihaveobject", "objectid": 5},
            {"type": "ihaveobject", "objectid": "ab" * 31},
        ]:
            with self.subTest(message=message):
                g1 = await self.ready_client()
                await g1.send(message)
                await self.assertError(g1, "INVALID_FORMAT")

    # -- storage --------------------------------------------------------------

    async def test_fresh_node_only_knows_genesis(self):
        self.assertEqual(self.node.objects.ids(), [GENESIS_ID])

    async def test_objects_survive_restart(self):
        g1 = await self.ready_client()
        tx = self.spend()
        await self.send_object(g1, self.coinbase)
        await self.send_object(g1, tx)
        await self.sync(g1)
        await self.node.stop()

        self.node = await self.start_node()
        g = await self.ready_client()
        await g.send({"type": "getobject", "objectid": txid(tx)})
        self.assertEqual((await g.recv())["object"], tx)
        # and the restored database is used for validation
        await self.send_object(g, self.spend(outputs=[(self.bob_pub, 1)]))
        await g.send({"type": "getobject", "objectid": "00" * 32})
        self.assertEqual((await g.recv())["name"], "UNKNOWN_OBJECT")


if __name__ == "__main__":
    unittest.main()
