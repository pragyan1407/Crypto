"""Task 3 integration tests: blocks over the wire, with two peers (Grader 1 and Grader 2)."""

import asyncio
import time

from blocktools import make_block, make_unmined, mine, mined_block
from fixtures import PDF_BLOCK, PDF_BLOCK_COINBASE, PDF_BLOCK_COINBASE_ID, PDF_BLOCK_ID
from objects import BLOCK_REWARD, GENESIS_ID, object_id
from test_node import NodeTestCase
from txtools import make_coinbase, make_tx, new_private_key, pubkey_hex, txid

T0 = 1671148800
R = BLOCK_REWARD


def ihave(objectid):
    return {"type": "ihaveobject", "objectid": objectid}


def getobject(objectid):
    return {"type": "getobject", "objectid": objectid}


class BlockNodeTestCase(NodeTestCase):
    async def start_node(self, **kwargs):
        kwargs.setdefault("fetch_timeout", 0.6)  # the protocol says 5 s; keep the tests quick
        return await super().start_node(**kwargs)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice, self.bob = new_private_key(), new_private_key()
        self.alice_pub, self.bob_pub = pubkey_hex(self.alice), pubkey_hex(self.bob)
        self.cb1 = make_coinbase(self.alice_pub, value=R, height=1)
        self.cb1_id = txid(self.cb1)
        self.b1 = mined_block([self.cb1_id], GENESIS_ID, created=T0)
        self.b1_id = object_id(self.b1)

    # -- helpers --------------------------------------------------------------

    async def send_object(self, client, obj):
        await client.send({"type": "object", "object": obj})

    async def sync(self, client):
        """Round trip proving the node processed everything we sent before (and that we are still connected)."""
        await client.send({"type": "getpeers"})
        self.assertEqual((await client.recv())["type"], "peers")

    async def silent(self, client, seconds=0.4):
        with self.assertRaises(asyncio.TimeoutError):
            await client.recv(timeout=seconds)

    async def load(self, *objects):
        """Store objects in the node via a throw-away client (nobody else is connected yet)."""
        loader = await self.ready_client()
        for obj in objects:
            await self.send_object(loader, obj)
        await self.sync(loader)

    async def load_chain(self):
        await self.load(self.cb1, self.b1)
        self.assertEqual(self.node.objects.block_state(self.b1_id)[0], 1)

    def block2(self, txids, **kwargs):
        kwargs.setdefault("created", T0 + 1)
        return mined_block(txids, self.b1_id, **kwargs)

    def coinbase2(self, value=R, height=2, pubkey=None):
        tx = make_coinbase(pubkey or self.bob_pub, value=value, height=height)
        return tx, txid(tx)

    def spend(self, outpoint_txid, key, outputs):
        tx = make_tx([(outpoint_txid, 0, key)], outputs)
        return tx, txid(tx)

    async def expect_fatal(self, client, name):
        msg = await client.recv()
        self.assertEqual((msg["type"], msg["name"]), ("error", name), msg)
        self.assertTrue(await client.expect_closed())


class BlockGossipTest(BlockNodeTestCase):
    async def test_valid_block_is_gossiped(self):
        g1, g2 = await self.ready_client(), await self.ready_client()
        await self.send_object(g1, self.cb1)
        await self.send_object(g1, self.b1)
        self.assertEqual(await g2.recv(), ihave(self.cb1_id))
        self.assertEqual(await g2.recv(), ihave(self.b1_id))
        await self.sync(g1)
        await self.silent(g1)  # the sender is not told about its own block
        self.assertEqual(self.node.objects.block_state(self.b1_id), (1, {(self.cb1_id, 0)}))

        await g2.send(getobject(self.b1_id))
        self.assertEqual(await g2.recv(), {"type": "object", "object": self.b1})

    async def test_pdf_example_block(self):
        g1, g2 = await self.ready_client(), await self.ready_client()
        await self.send_object(g1, PDF_BLOCK_COINBASE)
        await self.send_object(g1, PDF_BLOCK)
        self.assertEqual(await g2.recv(), ihave(PDF_BLOCK_COINBASE_ID))
        self.assertEqual(await g2.recv(), ihave(PDF_BLOCK_ID))
        self.assertEqual(self.node.objects.block_state(PDF_BLOCK_ID), (1, {(PDF_BLOCK_COINBASE_ID, 0)}))

    async def test_block_without_coinbase_and_block_with_fees(self):
        await self.load_chain()
        g1, g2 = await self.ready_client(), await self.ready_client()
        empty = self.block2([])
        await self.send_object(g1, empty)
        self.assertEqual(await g2.recv(), ihave(object_id(empty)))

        tx, tx_id = self.spend(self.cb1_id, self.alice, [(self.bob_pub, R - 5)])  # fee 5
        cb, cb_id = self.coinbase2(value=R + 5)
        block = self.block2([cb_id, tx_id], created=T0 + 2)
        for obj in (tx, cb, block):
            await self.send_object(g1, obj)
        self.assertEqual(await g2.recv(), ihave(tx_id))
        self.assertEqual(await g2.recv(), ihave(cb_id))
        self.assertEqual(await g2.recv(), ihave(object_id(block)))

    async def test_known_block_is_not_gossiped_again(self):
        g1, g2 = await self.ready_client(), await self.ready_client()
        await self.send_object(g1, self.cb1)
        await self.send_object(g1, self.b1)
        await g2.recv()
        await g2.recv()
        await self.send_object(g1, self.b1)
        await self.send_object(g2, self.b1)
        await self.sync(g1)
        await self.sync(g2)
        await self.silent(g2)

    async def test_ihaveobject_for_unknown_block_is_requested(self):
        g1 = await self.ready_client()
        await g1.send(ihave(self.b1_id))
        self.assertEqual(await g1.recv(), getobject(self.b1_id))


class FetchTransactionsTest(BlockNodeTestCase):
    async def test_missing_transaction_is_fetched(self):
        g1, g2 = await self.ready_client(), await self.ready_client()
        await self.send_object(g1, self.b1)  # the coinbase is not known
        self.assertEqual(await g1.recv(), getobject(self.cb1_id))
        await self.sync(g1)  # validation is pending, the connection keeps working
        self.assertFalse(self.node.objects.has(self.b1_id))
        await self.silent(g2)

        await self.send_object(g1, self.cb1)
        self.assertEqual(await g2.recv(), ihave(self.cb1_id))
        self.assertEqual(await g2.recv(), ihave(self.b1_id))
        self.assertEqual(self.node.objects.block_state(self.b1_id), (1, {(self.cb1_id, 0)}))
        await self.silent(g1)

    async def test_several_missing_transactions_answered_in_any_order(self):
        await self.load_chain()
        g1, g2 = await self.ready_client(), await self.ready_client()
        tx, tx_id = self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)])
        cb, cb_id = self.coinbase2()
        block = self.block2([cb_id, tx_id])
        await self.send_object(g1, block)
        requested = {(await g1.recv())["objectid"], (await g1.recv())["objectid"]}
        self.assertEqual(requested, {cb_id, tx_id})
        await self.send_object(g1, tx)  # answer in the opposite order
        await self.send_object(g1, cb)
        gossiped = [(await g2.recv())["objectid"] for _ in range(3)]
        self.assertEqual(sorted(gossiped[:2]), sorted([tx_id, cb_id]))
        self.assertEqual(gossiped[2], object_id(block))  # the block comes last

    async def test_block_sent_twice_while_pending_is_requested_once(self):
        g1 = await self.ready_client()
        await self.send_object(g1, self.b1)
        await self.send_object(g1, self.b1)
        self.assertEqual(await g1.recv(), getobject(self.cb1_id))
        await self.silent(g1)
        await self.send_object(g1, self.cb1)
        await self.sync(g1)
        self.assertTrue(self.node.objects.has(self.b1_id))

    async def test_unfindable_object_after_timeout_keeps_connection(self):
        g1, g2 = await self.ready_client(), await self.ready_client()
        await self.send_object(g1, self.b1)
        self.assertEqual(await g1.recv(), getobject(self.cb1_id))
        msg = await g1.recv(timeout=3)  # nothing is answered: give up after fetch_timeout
        self.assertEqual((msg["type"], msg["name"]), ("error", "UNFINDABLE_OBJECT"))
        await self.sync(g1)  # not treated as faulty
        await self.silent(g2)
        self.assertFalse(self.node.objects.has(self.b1_id))

        # the peer may try again later, now with the transaction
        await self.send_object(g1, self.cb1)
        await self.send_object(g1, self.b1)
        self.assertEqual(await g2.recv(), ihave(self.cb1_id))
        self.assertEqual(await g2.recv(), ihave(self.b1_id))

    async def test_invalid_fetched_transaction_invalidates_the_block(self):
        await self.load_chain()
        g1, g2 = await self.ready_client(), await self.ready_client()
        bad, bad_id = self.spend(self.cb1_id, self.bob, [(self.bob_pub, R)])  # signed with the wrong key
        block = self.block2([bad_id])
        await self.send_object(g1, block)
        self.assertEqual(await g1.recv(), getobject(bad_id))
        await self.send_object(g1, bad)
        first, second = await g1.recv(), await g1.recv()
        self.assertEqual(first["name"], "INVALID_TX_SIGNATURE")  # the transaction itself
        self.assertEqual(second["name"], "INVALID_ANCESTRY")  # the block that was waiting for it
        self.assertTrue(await g1.expect_closed())
        await self.silent(g2)
        self.assertFalse(self.node.objects.has(object_id(block)))
        self.assertFalse(self.node.objects.has(bad_id))

    async def test_invalid_ancestor_reported_to_other_peer_that_sent_the_block(self):
        await self.load_chain()
        g1, g2, g3 = [await self.ready_client() for _ in range(3)]
        bad, bad_id = self.spend(self.cb1_id, self.bob, [(self.bob_pub, R)])
        block = self.block2([bad_id])
        await self.send_object(g1, block)
        self.assertEqual(await g1.recv(), getobject(bad_id))
        await self.send_object(g3, bad)  # somebody else delivers the invalid transaction
        self.assertEqual((await g3.recv())["name"], "INVALID_TX_SIGNATURE")
        msg = await g1.recv()
        self.assertEqual(msg["name"], "INVALID_ANCESTRY")
        await self.sync(g1)  # the sender of the block is not disconnected for this
        await self.silent(g2)

    async def test_peer_disconnecting_while_block_is_pending(self):
        g1 = await self.ready_client()
        await self.send_object(g1, self.b1)
        self.assertEqual(await g1.recv(), getobject(self.cb1_id))
        await g1.close()
        await asyncio.sleep(0.9)  # longer than fetch_timeout: nothing must blow up
        g2 = await self.ready_client()
        await self.sync(g2)
        self.assertFalse(self.node.objects.has(self.b1_id))

    async def test_unknown_parent_is_not_fatal(self):
        g1, g2 = await self.ready_client(), await self.ready_client()
        orphan = mined_block([], "ab" * 32, created=T0)
        await self.send_object(g1, orphan)
        msg = await g1.recv()
        self.assertEqual((msg["type"], msg["name"]), ("error", "UNKNOWN_OBJECT"))
        await self.sync(g1)
        await self.silent(g2)

        # block B before its parent A is rejected, after A it is accepted
        await self.send_object(g1, self.cb1)
        child_cb, child_cb_id = self.coinbase2()
        child = self.block2([child_cb_id])
        await self.send_object(g1, child)
        self.assertEqual((await g1.recv())["name"], "UNKNOWN_OBJECT")
        await self.send_object(g1, self.b1)
        await self.send_object(g1, child_cb)
        await self.send_object(g1, child)
        await self.sync(g1)
        self.assertEqual(self.node.objects.block_state(object_id(child))[0], 2)


class InvalidBlockTest(BlockNodeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.load_chain()
        # transactions that are valid on their own (Task 2) and stored, but not confirmed anywhere
        self.cb2, self.cb2_id = self.coinbase2()
        self.cb2_big, self.cb2_big_id = self.coinbase2(value=R + 1, pubkey=self.alice_pub)
        self.cb2_h3, self.cb2_h3_id = self.coinbase2(height=3)
        self.tx_a, self.tx_a_id = self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)])
        self.tx_b, self.tx_b_id = self.spend(self.cb1_id, self.alice, [(self.alice_pub, R - 1)])  # double spend of tx_a
        self.tx_c, self.tx_c_id = self.spend(self.tx_a_id, self.bob, [(self.alice_pub, R)])
        self.tx_s, self.tx_s_id = self.spend(self.cb2_id, self.bob, [(self.alice_pub, R)])
        await self.load(self.cb2, self.cb2_big, self.cb2_h3, self.tx_a, self.tx_b, self.tx_c, self.tx_s)

    async def check_rejected(self, block, name):
        g1, g2 = await self.ready_client(), await self.ready_client()
        await self.send_object(g1, block)
        await self.expect_fatal(g1, name)
        await self.silent(g2)  # an invalid block is never gossiped
        self.assertFalse(self.node.objects.has(object_id(block)))

    async def test_invalid_blocks(self):
        wrong_target = self.block2([])
        wrong_target["T"] = "0000abd" + "0" * 57
        extra_key = dict(self.block2([]), extra=1)
        cases = {
            "(a) incorrect target": (wrong_target, "INVALID_FORMAT"),
            "extra key": (extra_key, "INVALID_FORMAT"),
            "(b) proof of work not valid": (make_unmined(make_block([], self.b1_id, created=T0 + 1)), "INVALID_BLOCK_POW"),
            "(d) two transactions spend the same output": (
                self.block2([self.cb2_id, self.tx_a_id, self.tx_b_id]),
                "INVALID_TX_OUTPOINT",
            ),
            "(e) output that is not in the UTXO set": (
                self.block2([self.cb2_id, self.tx_c_id]),
                "INVALID_TX_OUTPOINT",
            ),
            "(f) coinbase exceeds reward and fees": (self.block2([self.cb2_big_id]), "INVALID_BLOCK_COINBASE"),
            "coinbase height does not match": (self.block2([self.cb2_h3_id]), "INVALID_BLOCK_COINBASE"),
            "coinbase not first": (self.block2([self.tx_a_id, self.cb2_id]), "INVALID_BLOCK_COINBASE"),
            "two coinbases": (self.block2([self.cb2_id, self.cb2_big_id]), "INVALID_BLOCK_COINBASE"),
            "coinbase spent in the same block": (self.block2([self.cb2_id, self.tx_s_id]), "INVALID_TX_OUTPOINT"),
            "timestamp equal to parent": (self.block2([], created=T0), "INVALID_BLOCK_TIMESTAMP"),
            "timestamp before parent": (self.block2([], created=T0 - 5), "INVALID_BLOCK_TIMESTAMP"),
            "timestamp in the future": (
                self.block2([], created=int(time.time()) + 3600),
                "INVALID_BLOCK_TIMESTAMP",
            ),
            "second genesis block": (mine(make_block([], None, created=T0)), "INVALID_GENESIS"),
            "txids references a block": (self.block2([GENESIS_ID]), "INVALID_FORMAT"),
            "parent is a transaction": (mined_block([], self.cb1_id, created=T0 + 1), "INVALID_FORMAT"),
        }
        for name, (block, expected) in cases.items():
            with self.subTest(name):
                await self.check_rejected(block, expected)

    async def test_invalid_block_with_transactions_that_have_to_be_fetched(self):
        # same defects, but the node does not have the transactions and asks for them first
        cb_new, cb_new_id = self.coinbase2(value=R + 9, pubkey=self.bob_pub)  # not stored yet
        tx_d, tx_d_id = self.spend(self.cb1_id, self.alice, [(self.bob_pub, R - 3)])  # fee 3, not stored yet
        cases = {
            "coinbase too large": ([cb_new_id, tx_d_id], [cb_new, tx_d], "INVALID_BLOCK_COINBASE"),
        }
        for name, (txids, objects, expected) in cases.items():
            with self.subTest(name):
                g1, g2 = await self.ready_client(), await self.ready_client()
                block = self.block2(txids)
                await self.send_object(g1, block)
                requested = {(await g1.recv())["objectid"] for _ in txids}
                self.assertEqual(requested, set(txids))
                for obj in objects:
                    await self.send_object(g1, obj)
                await self.expect_fatal(g1, expected)
                self.assertFalse(self.node.objects.has(object_id(block)))
                # The fetched transactions are valid on their own, so they are gossiped; the block is not.
                gossiped = []
                while True:
                    try:
                        gossiped.append(await g2.recv(timeout=0.3))
                    except asyncio.TimeoutError:
                        break
                self.assertEqual(sorted(m["objectid"] for m in gossiped), sorted(txids))
                self.assertTrue(all(m["type"] == "ihaveobject" for m in gossiped))

    async def test_valid_siblings_after_invalid_blocks(self):
        # nothing from the rejected blocks above is left behind: a valid block on the same parent works
        g1, g2 = await self.ready_client(), await self.ready_client()
        good = self.block2([self.cb2_id, self.tx_a_id])
        await self.send_object(g1, good)
        self.assertEqual(await g2.recv(), ihave(object_id(good)))
        state = self.node.objects.block_state(object_id(good))
        self.assertEqual(state, (2, {(self.cb2_id, 0), (self.tx_a_id, 0)}))


class PersistenceTest(BlockNodeTestCase):
    async def test_chain_and_utxo_state_survive_restart(self):
        await self.load_chain()
        tx_a, tx_a_id = self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)])
        tx_b, tx_b_id = self.spend(self.cb1_id, self.alice, [(self.alice_pub, R - 1)])
        cb2, cb2_id = self.coinbase2()
        b2 = self.block2([cb2_id, tx_a_id])
        b2_id = object_id(b2)
        await self.load(cb2, tx_a, tx_b, b2)
        await self.node.stop()

        self.node = await self.start_node()
        self.assertEqual(self.node.objects.block_state(b2_id), (2, {(cb2_id, 0), (tx_a_id, 0)}))

        # cb1:0 was spent in b2, so a block on top of b2 cannot spend it again ...
        g1 = await self.ready_client()
        await self.send_object(g1, mined_block([tx_b_id], b2_id, created=T0 + 2))
        await self.expect_fatal(g1, "INVALID_TX_OUTPOINT")

        # ... but a block that spends tx_a's output is fine
        tx_next, tx_next_id = self.spend(tx_a_id, self.bob, [(self.alice_pub, R)])
        g2, g3 = await self.ready_client(), await self.ready_client()
        b3 = mined_block([tx_next_id], b2_id, created=T0 + 2, note="after restart")
        await self.send_object(g2, tx_next)
        await self.send_object(g2, b3)
        self.assertEqual(await g3.recv(), ihave(tx_next_id))
        self.assertEqual(await g3.recv(), ihave(object_id(b3)))
        self.assertEqual(self.node.objects.block_state(object_id(b3))[0], 3)
