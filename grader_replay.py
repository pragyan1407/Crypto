"""Replay the Task 1, 2 and 3 grader scenarios against a node that is already running.

Usage:
    python grader_replay.py [host] [port] [--fast]

Example (after `docker-compose up -d`):
    python grader_replay.py localhost 18018

--fast skips the slow cases: the incomplete hello (20 s handshake timeout)
and the missing-transactions case (5 s fetch timeout).
"""

import asyncio
import json
import sys

HELLO = b'{"agent":"Grader","type":"hello","version":"0.10.0"}\n'
GETPEERS = b'{"type":"getpeers"}\n'
INVALID_FIRST_MESSAGES = [
    b"hgjhlkjhlkj{{\n",
    b'{"typ":"diufygeuybhv"}\n',
    b'{"type":"hello"}\n',
    b'{"type":"hello", "version":"jd3.x"}\n',
    b'{"type":"hello", "version":"0.8.0"}\n',
]


class Conn:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def open(cls, host, port):
        return cls(*await asyncio.open_connection(host, port))

    async def send(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self, timeout=5.0):
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        check(line, "connection closed unexpectedly")
        return json.loads(line)

    async def closed(self, timeout=5.0):
        return await asyncio.wait_for(self.reader.read(), timeout) == b""

    def close(self):
        self.writer.close()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


async def expect_hello_and_getpeers(conn):
    hello = await conn.recv()
    check(hello.get("type") == "hello", f"first message is not hello: {hello}")
    check(set(hello) == {"type", "version", "agent"}, f"bad hello keys: {hello}")
    check(hello["version"].startswith("0.10."), f"bad version: {hello}")
    check(await conn.recv() == {"type": "getpeers"}, "second message is not getpeers")


def check_peers(msg):
    check(msg.get("type") == "peers" and set(msg) == {"type", "peers"}, f"bad peers message: {msg}")
    check(isinstance(msg["peers"], list) and len(msg["peers"]) <= 30, "peers must be a list of at most 30")


async def case_connect_and_hello(host, port):
    c = await Conn.open(host, port)
    await expect_hello_and_getpeers(c)
    c.close()


async def case_reconnect(host, port):
    for _ in range(2):
        c = await Conn.open(host, port)
        await expect_hello_and_getpeers(c)
        c.close()


async def case_getpeers(host, port):
    c = await Conn.open(host, port)
    await c.send(HELLO + GETPEERS)
    await expect_hello_and_getpeers(c)
    check_peers(await c.recv())
    c.close()


async def case_fragmented_getpeers(host, port):
    c = await Conn.open(host, port)
    await c.send(HELLO + b'{"type":"g')
    await expect_hello_and_getpeers(c)
    await asyncio.sleep(0.1)
    await c.send(b'etpeers"}\n')
    check_peers(await c.recv())
    c.close()


async def case_message_before_hello(host, port):
    c = await Conn.open(host, port)
    await c.send(GETPEERS)
    await expect_hello_and_getpeers(c)
    msg = await c.recv()
    check(msg.get("type") == "error" and msg.get("name") == "INVALID_HANDSHAKE", f"expected INVALID_HANDSHAKE, got {msg}")
    check(await c.closed(), "node did not disconnect")


async def case_peers_come_back(host, port):
    c = await Conn.open(host, port)
    await c.send(HELLO + GETPEERS + b'{"peers":["host.docker.internal:18019"],"type":"peers"}\n')
    await expect_hello_and_getpeers(c)
    check_peers(await c.recv())
    await asyncio.sleep(2.0)
    await c.send(GETPEERS)
    msg = await c.recv()
    check_peers(msg)
    check("host.docker.internal:18019" in msg["peers"], f"peer we sent is missing: {msg}")
    c.close()


async def case_invalid_hello_messages(host, port):
    for line in INVALID_FIRST_MESSAGES:
        c = await Conn.open(host, port)
        await c.send(line)
        await expect_hello_and_getpeers(c)
        msg = await c.recv()
        check(
            msg.get("type") == "error" and msg.get("name") in ("INVALID_FORMAT", "INVALID_HANDSHAKE"),
            f"{line!r}: expected an error, got {msg}",
        )
        check(await c.closed(), f"{line!r}: node did not disconnect")


async def case_two_connections(host, port):
    a = await Conn.open(host, port)
    b = await Conn.open(host, port)
    await expect_hello_and_getpeers(a)
    await expect_hello_and_getpeers(b)
    a.close()
    b.close()


async def case_incomplete_hello(host, port):
    c = await Conn.open(host, port)
    await expect_hello_and_getpeers(c)
    await c.send(b'{"type":"hello", "ver')
    msg = await c.recv(timeout=35.0)
    check(
        msg.get("type") == "error" and msg.get("name") in ("INVALID_FORMAT", "INVALID_HANDSHAKE"),
        f"expected an error, got {msg}",
    )
    check(await c.closed(), "node did not disconnect")


async def ready_conn(host, port):
    c = await Conn.open(host, port)
    await c.send(HELLO)
    await expect_hello_and_getpeers(c)
    return c


async def send_json(conn, obj):
    await conn.send(json.dumps(obj, separators=(",", ":")).encode() + b"\n")


async def expect_silence(conn, seconds=0.5):
    try:
        line = await asyncio.wait_for(conn.reader.readline(), seconds)
    except asyncio.TimeoutError:
        return
    check(False, f"expected no message, got {line!r}")


def fresh_transactions():
    """A new coinbase and a valid spend of it (new keys every run, so the node has never seen them)."""
    from txtools import make_coinbase, make_tx, new_private_key, pubkey_hex, txid

    alice, bob = new_private_key(), new_private_key()
    coinbase = make_coinbase(pubkey_hex(alice), value=1000)
    spend = make_tx([(txid(coinbase), 0, alice)], [(pubkey_hex(bob), 1000)])
    return coinbase, spend, txid(coinbase), txid(spend), alice, bob


async def case_object_exchange(host, port):
    from txtools import txid

    coinbase, spend, cb_id, tx_id, _, _ = fresh_transactions()
    g1 = await ready_conn(host, port)
    g2 = await ready_conn(host, port)
    await send_json(g1, {"type": "object", "object": coinbase})
    await send_json(g1, {"type": "object", "object": spend})
    # Grader 2 must be told about both objects ...
    check(await g2.recv() == {"type": "ihaveobject", "objectid": cb_id}, "Grader 2 did not get ihaveobject for coinbase")
    check(await g2.recv() == {"type": "ihaveobject", "objectid": tx_id}, "Grader 2 did not get ihaveobject for transaction")
    # ... and both can fetch them
    await send_json(g1, {"type": "getobject", "objectid": tx_id})
    check(await g1.recv() == {"type": "object", "object": spend}, "Grader 1 did not get its object back")
    await send_json(g2, {"type": "getobject", "objectid": tx_id})
    check(await g2.recv() == {"type": "object", "object": spend}, "Grader 2 did not get the object")
    check(txid(spend) == tx_id, "id mismatch")


async def case_ihaveobject_triggers_getobject(host, port):
    g1 = await ready_conn(host, port)
    unknown = "ab" * 32
    await send_json(g1, {"type": "ihaveobject", "objectid": unknown})
    check(await g1.recv() == {"type": "getobject", "objectid": unknown}, "no getobject after ihaveobject")


async def case_unknown_object(host, port):
    g1 = await ready_conn(host, port)
    await send_json(g1, {"type": "getobject", "objectid": "cd" * 32})
    msg = await g1.recv()
    check(msg.get("type") == "error" and msg.get("name") == "UNKNOWN_OBJECT", f"expected UNKNOWN_OBJECT, got {msg}")
    await send_json(g1, {"type": "getpeers"})
    check((await g1.recv()).get("type") == "peers", "connection should stay open after UNKNOWN_OBJECT")


async def case_invalid_transactions(host, port):
    from txtools import make_tx, pubkey_hex, new_private_key

    coinbase, spend, cb_id, _, alice, bob = fresh_transactions()
    g2 = await ready_conn(host, port)
    setup = await ready_conn(host, port)
    await send_json(setup, {"type": "object", "object": coinbase})
    await g2.recv()  # gossip about the coinbase
    bob_pub = pubkey_hex(bob)
    negative = make_tx([(cb_id, 0, alice)], [(bob_pub, 1)])
    negative["outputs"][0]["value"] = -1
    cases = {
        "wrong signature": (make_tx([(cb_id, 0, new_private_key())], [(bob_pub, 1)]), "INVALID_TX_SIGNATURE"),
        "outputs exceed inputs": (make_tx([(cb_id, 0, alice)], [(bob_pub, 1001)]), "INVALID_TX_CONSERVATION"),
        "bad index": (make_tx([(cb_id, 7, alice)], [(bob_pub, 1)]), "INVALID_TX_OUTPOINT"),
        "negative value": (negative, "INVALID_FORMAT"),
        "double spend in one tx": (make_tx([(cb_id, 0, alice), (cb_id, 0, alice)], [(bob_pub, 1)]), "INVALID_TX_CONSERVATION"),
    }
    for name, (tx, expected) in cases.items():
        g1 = await ready_conn(host, port)
        await send_json(g1, {"type": "object", "object": tx})
        msg = await g1.recv()
        check(msg.get("type") == "error" and msg.get("name") == expected, f"{name}: expected {expected}, got {msg}")
        check(await g1.closed(), f"{name}: node did not disconnect")
        await expect_silence(g2)  # invalid transactions must not be gossiped


async def case_unknown_ancestor(host, port):
    coinbase, spend, cb_id, tx_id, _, _ = fresh_transactions()
    g1 = await ready_conn(host, port)
    await send_json(g1, {"type": "object", "object": spend})  # coinbase was never sent
    msg = await g1.recv()
    check(msg.get("type") == "error" and msg.get("name") == "UNKNOWN_OBJECT", f"expected UNKNOWN_OBJECT, got {msg}")
    await send_json(g1, {"type": "getpeers"})
    check((await g1.recv()).get("type") == "peers", "connection should stay open")


class Chain:
    """Fresh keys, a coinbase and a block 1 on top of genesis (new ids every run, so a node never saw them)."""

    def __init__(self):
        import time

        from blocktools import mined_block
        from objects import BLOCK_REWARD, GENESIS_ID, object_id
        from txtools import make_coinbase, new_private_key, pubkey_hex, txid

        self.reward = BLOCK_REWARD
        self.alice, self.bob = new_private_key(), new_private_key()
        self.alice_pub, self.bob_pub = pubkey_hex(self.alice), pubkey_hex(self.bob)
        self.cb1 = make_coinbase(self.alice_pub, value=BLOCK_REWARD, height=1)
        self.cb1_id = txid(self.cb1)
        self.base = int(time.time()) - 1000
        self.b1 = mined_block([self.cb1_id], GENESIS_ID, created=self.base)
        self.b1_id = object_id(self.b1)
        self._mined_block, self._txid, self._object_id = mined_block, txid, object_id

    def block2(self, txids, created=None):
        return self._mined_block(txids, self.b1_id, created=self.base + 1 if created is None else created)

    def spend(self, outpoint_txid, key, outputs):
        from txtools import make_tx

        tx = make_tx([(outpoint_txid, 0, key)], outputs)
        return tx, self._txid(tx)

    def coinbase(self, value, height=2, pubkey=None):
        from txtools import make_coinbase

        tx = make_coinbase(pubkey or self.bob_pub, value=value, height=height)
        return tx, self._txid(tx)


def obj(o):
    return {"type": "object", "object": o}


async def drain(conn, seconds=0.4):
    """All messages that arrive within `seconds`."""
    messages = []
    while True:
        try:
            messages.append(await conn.recv(timeout=seconds))
        except asyncio.TimeoutError:
            return messages


async def case_block_gossip(host, port):
    c = Chain()
    g1, g2 = await ready_conn(host, port), await ready_conn(host, port)
    await send_json(g1, obj(c.cb1))
    await send_json(g1, obj(c.b1))
    check(await g2.recv() == {"type": "ihaveobject", "objectid": c.cb1_id}, "Grader 2 got no ihaveobject for the coinbase")
    check(await g2.recv() == {"type": "ihaveobject", "objectid": c.b1_id}, "Grader 2 got no ihaveobject for the block")
    await send_json(g2, {"type": "getobject", "objectid": c.b1_id})
    check(await g2.recv() == obj(c.b1), "Grader 2 did not get the block")


async def case_block_fetches_transactions(host, port):
    c = Chain()
    g1, g2 = await ready_conn(host, port), await ready_conn(host, port)
    await send_json(g1, obj(c.b1))  # the node has never seen the coinbase
    check(await g1.recv() == {"type": "getobject", "objectid": c.cb1_id}, "node did not ask for the coinbase")
    await send_json(g1, obj(c.cb1))
    check((await g2.recv())["objectid"] == c.cb1_id, "Grader 2 got no ihaveobject for the coinbase")
    check((await g2.recv())["objectid"] == c.b1_id, "Grader 2 got no ihaveobject for the block")


async def case_invalid_blocks(host, port):
    from blocktools import make_block, make_unmined
    import time

    c = Chain()
    loader = await ready_conn(host, port)
    for o in (c.cb1, c.b1):
        await send_json(loader, obj(o))
    cb2, cb2_id = c.coinbase(c.reward)
    cb_big, cb_big_id = c.coinbase(c.reward + 1, pubkey=c.alice_pub)
    tx_a, tx_a_id = c.spend(c.cb1_id, c.alice, [(c.bob_pub, c.reward)])
    tx_b, tx_b_id = c.spend(c.cb1_id, c.alice, [(c.alice_pub, c.reward - 1)])
    tx_c, tx_c_id = c.spend(tx_a_id, c.bob, [(c.alice_pub, c.reward)])
    for o in (cb2, cb_big, tx_a, tx_b, tx_c):
        await send_json(loader, obj(o))
    await send_json(loader, {"type": "getpeers"})
    while (await loader.recv()).get("type") != "peers":
        pass

    wrong_target = c.block2([])
    wrong_target["T"] = "0000abd" + "0" * 57
    cases = {
        "(a) incorrect target": (wrong_target, "INVALID_FORMAT"),
        "(b) invalid proof of work": (make_unmined(make_block([], c.b1_id, created=c.base + 1)), "INVALID_BLOCK_POW"),
        "(d) two transactions spend the same output": (c.block2([cb2_id, tx_a_id, tx_b_id]), "INVALID_TX_OUTPOINT"),
        "(e) output not in the UTXO set": (c.block2([cb2_id, tx_c_id]), "INVALID_TX_OUTPOINT"),
        "(f) coinbase exceeds reward and fees": (c.block2([cb_big_id]), "INVALID_BLOCK_COINBASE"),
        "timestamp not after parent": (c.block2([], created=c.base), "INVALID_BLOCK_TIMESTAMP"),
        "timestamp in the future": (c.block2([], created=int(time.time()) + 3600), "INVALID_BLOCK_TIMESTAMP"),
    }
    g2 = await ready_conn(host, port)
    for name, (block, expected) in cases.items():
        g1 = await ready_conn(host, port)
        await send_json(g1, obj(block))
        msg = await g1.recv()
        check(msg.get("type") == "error" and msg.get("name") == expected, f"{name}: expected {expected}, got {msg}")
        check(await g1.closed(), f"{name}: node did not disconnect")
        await expect_silence(g2)  # invalid blocks must not be gossiped


async def case_invalid_fetched_transaction(host, port):
    c = Chain()
    loader = await ready_conn(host, port)
    for o in (c.cb1, c.b1):
        await send_json(loader, obj(o))
    await send_json(loader, {"type": "getpeers"})
    while (await loader.recv()).get("type") != "peers":
        pass
    bad, bad_id = c.spend(c.cb1_id, c.bob, [(c.bob_pub, c.reward)])  # signed with the wrong key
    g1 = await ready_conn(host, port)
    await send_json(g1, obj(c.block2([bad_id])))
    check(await g1.recv() == {"type": "getobject", "objectid": bad_id}, "node did not ask for the transaction")
    await send_json(g1, obj(bad))
    names = [(await g1.recv()).get("name"), (await g1.recv()).get("name")]
    check(names == ["INVALID_TX_SIGNATURE", "INVALID_ANCESTRY"], f"unexpected errors {names}")


async def case_unknown_parent(host, port):
    from blocktools import mined_block

    c = Chain()
    g1 = await ready_conn(host, port)
    await send_json(g1, obj(mined_block([], "ab" * 32, created=c.base)))
    msg = await g1.recv()
    check(msg.get("type") == "error" and msg.get("name") == "UNKNOWN_OBJECT", f"expected UNKNOWN_OBJECT, got {msg}")
    await send_json(g1, {"type": "getpeers"})
    check((await g1.recv()).get("type") == "peers", "connection should stay open")


async def case_unfindable_object(host, port):
    c = Chain()
    g1 = await ready_conn(host, port)
    await send_json(g1, obj(c.b1))
    check(await g1.recv() == {"type": "getobject", "objectid": c.cb1_id}, "node did not ask for the coinbase")
    msg = await g1.recv(timeout=10.0)  # we never answer: the node gives up after 5 seconds
    check(msg.get("type") == "error" and msg.get("name") == "UNFINDABLE_OBJECT", f"expected UNFINDABLE_OBJECT, got {msg}")
    await send_json(g1, {"type": "getpeers"})
    check((await g1.recv()).get("type") == "peers", "connection should stay open")


CASES = [
    ("connect, hello, getpeers", case_connect_and_hello, False),
    ("reconnect", case_reconnect, False),
    ("getpeers -> peers", case_getpeers, False),
    ("fragmented getpeers", case_fragmented_getpeers, False),
    ("message before hello -> INVALID_HANDSHAKE", case_message_before_hello, False),
    ("peers sent earlier come back", case_peers_come_back, False),
    ("invalid hello messages -> INVALID_FORMAT", case_invalid_hello_messages, False),
    ("two parallel connections", case_two_connections, False),
    ("object exchange + gossip between two peers", case_object_exchange, False),
    ("ihaveobject -> getobject", case_ihaveobject_triggers_getobject, False),
    ("getobject for unknown id -> UNKNOWN_OBJECT", case_unknown_object, False),
    ("invalid transactions rejected, not gossiped", case_invalid_transactions, False),
    ("unknown ancestor -> UNKNOWN_OBJECT, stays connected", case_unknown_ancestor, False),
    ("valid block is gossiped", case_block_gossip, False),
    ("block with unknown transactions: node fetches them", case_block_fetches_transactions, False),
    ("invalid blocks rejected, not gossiped", case_invalid_blocks, False),
    ("invalid fetched transaction -> tx error + INVALID_ANCESTRY", case_invalid_fetched_transaction, False),
    ("unknown parent -> UNKNOWN_OBJECT, stays connected", case_unknown_parent, False),
    ("transactions never arrive -> UNFINDABLE_OBJECT after 5 s (slow)", case_unfindable_object, True),
    ("incomplete hello -> error after timeout (about 20 s)", case_incomplete_hello, True),
]


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fast = "--fast" in sys.argv
    host = args[0] if args else "localhost"
    port = int(args[1]) if len(args) > 1 else 18018

    failures = 0
    for name, case, slow in CASES:
        if slow and fast:
            print(f"SKIP  {name}")
            continue
        try:
            await case(host, port)
            print(f"PASS  {name}")
        except Exception as err:  # noqa: BLE001 - report everything
            failures += 1
            print(f"FAIL  {name}: {type(err).__name__}: {err}")
    print("all passed" if not failures else f"{failures} case(s) failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
