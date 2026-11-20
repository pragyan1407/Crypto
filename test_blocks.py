import copy
import unittest

from blocktools import make_block, make_unmined, mine, mined_block
from fixtures import PDF_BLOCK, PDF_BLOCK_COINBASE, PDF_BLOCK_COINBASE_ID, PDF_BLOCK_ID
from objects import BLOCK_REWARD, BLOCK_TARGET, GENESIS, GENESIS_ID, check_block_syntax, object_id
from objectstore import ObjectStore
from protocol import (
    INVALID_BLOCK_COINBASE,
    INVALID_BLOCK_POW,
    INVALID_BLOCK_TIMESTAMP,
    INVALID_FORMAT,
    INVALID_GENESIS,
    INVALID_TX_OUTPOINT,
    UNKNOWN_OBJECT,
    ProtocolError,
)
from txtools import make_coinbase, make_tx, new_private_key, pubkey_hex, txid
from validation import validate_block_body, validate_block_header, validate_object

NOW = 2_000_000_000  # the "current time" used for header checks
T0 = 1671148800  # created time of the first test block (later than genesis, long ago)
R = BLOCK_REWARD


class SyntaxTest(unittest.TestCase):
    def assertInvalid(self, block):
        with self.assertRaises(ProtocolError) as ctx:
            check_block_syntax(block)
        self.assertEqual(ctx.exception.name, INVALID_FORMAT, ctx.exception.msg)

    def test_valid_blocks(self):
        check_block_syntax(PDF_BLOCK)
        check_block_syntax(GENESIS)
        minimal = {k: v for k, v in PDF_BLOCK.items() if k not in ("miner", "note")}
        check_block_syntax(minimal)
        check_block_syntax(dict(PDF_BLOCK, miner="", note="a" * 128))
        check_block_syntax(dict(PDF_BLOCK, miner="x" * 128, note=" ~"))  # 0x20 and 0x7e are printable

    def test_missing_keys(self):
        for key in ("type", "txids", "nonce", "previd", "created", "T"):
            with self.subTest(key):
                self.assertInvalid({k: v for k, v in PDF_BLOCK.items() if k != key})

    def test_malformed_blocks(self):
        mutations = {
            "extra key": {"extra": 1},
            "txids not a list": {"txids": "ab"},
            "txids null": {"txids": None},
            "txid number": {"txids": [5]},
            "txid short": {"txids": [PDF_BLOCK_COINBASE_ID[:-2]]},
            "txid uppercase": {"txids": [PDF_BLOCK_COINBASE_ID.upper()]},
            "txid not hex": {"txids": ["zz" * 32]},
            "txid null": {"txids": [None]},
            "nonce short": {"nonce": "00" * 31},
            "nonce long": {"nonce": "00" * 33},
            "nonce uppercase": {"nonce": "AB" * 32},
            "nonce number": {"nonce": 5},
            "previd number": {"previd": 5},
            "previd short": {"previd": GENESIS_ID[:-2]},
            "previd uppercase": {"previd": GENESIS_ID.upper()},
            "created string": {"created": "1671148800"},
            "created float": {"created": 1671148800.0},
            "created bool": {"created": True},
            "created null": {"created": None},
            "created negative": {"created": -1},
            "target lower": {"T": "0000abb" + "0" * 57},
            "target higher": {"T": "0000abd" + "0" * 57},
            "target easy": {"T": "f" * 64},
            "target uppercase": {"T": BLOCK_TARGET.upper()},
            "target short": {"T": BLOCK_TARGET[:-2]},
            "target number": {"T": 5},
            "miner number": {"miner": 5},
            "miner null": {"miner": None},
            "miner non-ascii": {"miner": "gräder"},
            "miner too long": {"miner": "x" * 129},
            "miner control char": {"miner": "a\tb"},
            "note newline": {"note": "line1\nline2"},
            "note too long": {"note": "n" * 129},
            "note delete char": {"note": "a\x7f"},
        }
        for name, change in mutations.items():
            with self.subTest(name):
                self.assertInvalid(dict(PDF_BLOCK, **change))

    def test_not_an_object(self):
        for bad in [[], "block", 5, None]:
            with self.subTest(bad=bad):
                self.assertInvalid(bad)


class BlockTestCase(unittest.TestCase):
    """A store holding genesis and block 1 (coinbase of alice, height 1); helpers to build more."""

    def setUp(self):
        self.store = ObjectStore(":memory:")
        self.addCleanup(self.store.close)
        self.alice, self.bob = new_private_key(), new_private_key()
        self.alice_pub, self.bob_pub = pubkey_hex(self.alice), pubkey_hex(self.bob)
        self.cb1 = make_coinbase(self.alice_pub, value=R, height=1)
        self.cb1_id = self.add_tx(self.cb1)
        self.b1 = mined_block([self.cb1_id], GENESIS_ID, created=T0)
        self.b1_id = self.add_block(self.b1)

    # -- helpers --------------------------------------------------------------

    def add_tx(self, tx):
        """Store a transaction the way the node does: validated first."""
        validate_object(tx, self.store)
        tid = txid(tx)
        self.store.put(tid, tx)
        return tid

    def run_block(self, block):
        missing = validate_block_header(block, self.store, now=NOW)
        self.assertEqual(missing, [], "all transactions should be stored")
        return validate_block_body(block, self.store)

    def add_block(self, block):
        height, utxo = self.run_block(block)
        blockid = object_id(block)
        self.store.put_block(blockid, block, height, utxo)
        return blockid

    def block(self, txids, previd=None, created=None):
        return mined_block(txids, previd or self.b1_id, created=T0 + 1 if created is None else created)

    def assertInvalid(self, block, name):
        with self.assertRaises(ProtocolError) as ctx:
            self.run_block(block)
        self.assertEqual(ctx.exception.name, name, ctx.exception.msg)

    def spend(self, outpoint_txid, key, outputs, index=0):
        return make_tx([(outpoint_txid, index, key)], outputs)


class HeaderTest(BlockTestCase):
    def test_pdf_example_block(self):
        store = ObjectStore(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(object_id(PDF_BLOCK), PDF_BLOCK_ID)
        # the coinbase is unknown: it has to be fetched
        self.assertEqual(validate_block_header(PDF_BLOCK, store, now=NOW), [PDF_BLOCK_COINBASE_ID])
        store.put(PDF_BLOCK_COINBASE_ID, PDF_BLOCK_COINBASE)
        self.assertEqual(validate_block_header(PDF_BLOCK, store, now=NOW), [])
        height, utxo = validate_block_body(PDF_BLOCK, store)
        self.assertEqual(height, 1)
        self.assertEqual(utxo, {(PDF_BLOCK_COINBASE_ID, 0)})

    def test_missing_transactions_are_listed_once_in_order(self):
        a, b, c = "aa" * 32, "bb" * 32, "cc" * 32
        block = self.block([a, b, a, self.cb1_id, c])
        self.assertEqual(validate_block_header(block, self.store, now=NOW), [a, b, c])

    def test_wrong_target_is_a_format_error(self):
        block = mine(make_block([], self.b1_id, created=T0 + 1))
        block["T"] = "0000abd" + "0" * 57
        with self.assertRaises(ProtocolError) as ctx:
            validate_block_header(block, self.store, now=NOW)
        self.assertEqual(ctx.exception.name, INVALID_FORMAT)

    def test_proof_of_work(self):
        unmined = make_unmined(make_block([], self.b1_id, created=T0 + 1))
        self.assertInvalid(unmined, INVALID_BLOCK_POW)
        # PoW is checked before the parent is looked up
        unmined = make_unmined(make_block([], "ab" * 32, created=T0 + 1))
        self.assertInvalid(unmined, INVALID_BLOCK_POW)

    def test_genesis_rules(self):
        impostor = mine(make_block([], None, created=T0))
        self.assertInvalid(impostor, INVALID_GENESIS)
        self.assertInvalid(make_unmined(make_block([], None, created=T0)), INVALID_GENESIS)
        self.assertInvalid(dict(GENESIS, nonce="00" * 32), INVALID_GENESIS)

    def test_unknown_parent(self):
        self.assertInvalid(self.block([], previd="ab" * 32), UNKNOWN_OBJECT)

    def test_parent_must_be_a_block(self):
        self.assertInvalid(self.block([], previd=self.cb1_id), INVALID_FORMAT)

    def test_timestamps(self):
        self.assertInvalid(self.block([], created=T0), INVALID_BLOCK_TIMESTAMP)  # equal to the parent
        self.assertInvalid(self.block([], created=T0 - 1), INVALID_BLOCK_TIMESTAMP)  # older
        self.assertInvalid(self.block([], created=NOW + 1), INVALID_BLOCK_TIMESTAMP)  # in the future
        self.run_block(self.block([], created=T0 + 1))  # strictly later: fine
        self.run_block(self.block([], created=NOW))  # "now" itself is not in the future

    def test_timestamp_of_first_block_is_checked_against_genesis(self):
        too_old = mined_block([self.cb1_id], GENESIS_ID, created=GENESIS["created"])
        with self.assertRaises(ProtocolError) as ctx:
            validate_block_header(too_old, self.store, now=NOW)
        self.assertEqual(ctx.exception.name, INVALID_BLOCK_TIMESTAMP)


class CoinbaseTest(BlockTestCase):
    def cb(self, value=R, height=2, pubkey=None):
        return self.add_tx(make_coinbase(pubkey or self.bob_pub, value=value, height=height))

    def test_block_without_coinbase_is_valid(self):
        height, utxo = self.run_block(self.block([]))
        self.assertEqual((height, utxo), (2, {(self.cb1_id, 0)}))

    def test_valid_coinbase(self):
        cb2 = self.cb()
        height, utxo = self.run_block(self.block([cb2]))
        self.assertEqual(height, 2)
        self.assertEqual(utxo, {(self.cb1_id, 0), (cb2, 0)})

    def test_coinbase_value_may_be_less_than_the_reward(self):
        self.run_block(self.block([self.cb(value=R - 1)]))
        self.run_block(self.block([self.cb(value=0, pubkey=self.alice_pub)]))

    def test_coinbase_too_large(self):
        self.assertInvalid(self.block([self.cb(value=R + 1)]), INVALID_BLOCK_COINBASE)

    def test_coinbase_may_collect_fees(self):
        tx = self.spend(self.cb1_id, self.alice, [(self.bob_pub, R - 7)])  # fee of 7
        tx_id = self.add_tx(tx)
        self.run_block(self.block([self.cb(value=R + 7), tx_id]))
        self.assertInvalid(self.block([self.cb(value=R + 8, pubkey=self.alice_pub), tx_id]), INVALID_BLOCK_COINBASE)

    def test_fees_of_transactions_that_are_not_in_the_block_do_not_count(self):
        self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R - 7)]))
        self.assertInvalid(self.block([self.cb(value=R + 7)]), INVALID_BLOCK_COINBASE)

    def test_wrong_height(self):
        for height in (0, 1, 3):
            with self.subTest(height):
                self.assertInvalid(self.block([self.cb(height=height)]), INVALID_BLOCK_COINBASE)

    def test_coinbase_must_be_first(self):
        tx_id = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        self.assertInvalid(self.block([tx_id, self.cb()]), INVALID_BLOCK_COINBASE)

    def test_only_one_coinbase(self):
        self.assertInvalid(self.block([self.cb(), self.cb(pubkey=self.alice_pub)]), INVALID_BLOCK_COINBASE)
        cb2 = self.cb()
        self.assertInvalid(self.block([cb2, cb2]), INVALID_BLOCK_COINBASE)

    def test_coinbase_cannot_be_spent_in_the_same_block(self):
        cb2 = self.cb()
        spend = self.add_tx(self.spend(cb2, self.bob, [(self.alice_pub, 1000)]))
        self.assertInvalid(self.block([cb2, spend]), INVALID_TX_OUTPOINT)

    def test_coinbase_can_be_spent_in_a_later_block(self):
        cb2 = self.cb()
        b2 = self.block([cb2])
        b2_id = self.add_block(b2)
        spend = self.add_tx(self.spend(cb2, self.bob, [(self.alice_pub, R)]))
        height, utxo = self.run_block(mined_block([spend], b2_id, created=T0 + 2))
        self.assertEqual((height, utxo), (3, {(self.cb1_id, 0), (spend, 0)}))

    def test_txid_that_is_a_block(self):
        self.assertInvalid(self.block([GENESIS_ID]), INVALID_FORMAT)

    def test_transaction_that_is_not_stored(self):
        block = self.block(["ab" * 32])
        self.assertEqual(validate_block_header(block, self.store, now=NOW), ["ab" * 32])
        with self.assertRaises(ProtocolError) as ctx:
            validate_block_body(block, self.store)
        self.assertEqual(ctx.exception.name, UNKNOWN_OBJECT)


class UtxoTest(BlockTestCase):
    def test_spending_an_unspent_output(self):
        tx = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, 10), (self.alice_pub, R - 10)]))
        height, utxo = self.run_block(self.block([tx]))
        self.assertEqual(utxo, {(tx, 0), (tx, 1)})  # (u \ i) U o
        self.assertEqual(height, 2)

    def test_output_that_is_not_in_the_chain(self):
        # tx_a is a valid transaction, but never confirmed in a block on this chain
        tx_a = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        tx_c = self.add_tx(self.spend(tx_a, self.bob, [(self.alice_pub, R)]))
        self.assertInvalid(self.block([tx_c]), INVALID_TX_OUTPOINT)

    def test_two_transactions_spending_the_same_output(self):
        tx_a = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        tx_b = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.alice_pub, R - 1)]))
        self.run_block(self.block([tx_a]))  # either one alone is fine ...
        self.run_block(self.block([tx_b]))
        self.assertInvalid(self.block([tx_a, tx_b]), INVALID_TX_OUTPOINT)  # ... but not both

    def test_same_transaction_twice(self):
        tx = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        self.assertInvalid(self.block([tx, tx]), INVALID_TX_OUTPOINT)

    def test_spending_an_output_that_was_spent_in_an_earlier_block(self):
        tx_a = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        b2_id = self.add_block(self.block([tx_a]))
        tx_b = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.alice_pub, R - 1)]))
        self.assertInvalid(mined_block([tx_b], b2_id, created=T0 + 2), INVALID_TX_OUTPOINT)
        # on the sibling chain (parent b1) it is still unspent
        self.run_block(self.block([tx_b]))

    def test_spending_from_a_transaction_of_the_same_block(self):
        tx_a = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        tx_b = self.add_tx(self.spend(tx_a, self.bob, [(self.alice_pub, R)]))
        height, utxo = self.run_block(self.block([tx_a, tx_b]))
        self.assertEqual(utxo, {(tx_b, 0)})

    def test_order_matters(self):
        tx_a = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        tx_b = self.add_tx(self.spend(tx_a, self.bob, [(self.alice_pub, R)]))
        self.assertInvalid(self.block([tx_b, tx_a]), INVALID_TX_OUTPOINT)

    def test_several_inputs(self):
        cb2 = self.add_tx(make_coinbase(self.bob_pub, value=R, height=2))
        b2_id = self.add_block(self.block([cb2]))
        tx = make_tx([(self.cb1_id, 0, self.alice), (cb2, 0, self.bob)], [(self.alice_pub, 2 * R - 3)])
        tx_id = self.add_tx(tx)
        cb3 = self.add_tx(make_coinbase(self.alice_pub, value=R + 3, height=3))
        height, utxo = self.run_block(mined_block([cb3, tx_id], b2_id, created=T0 + 2))
        self.assertEqual(utxo, {(tx_id, 0), (cb3, 0)})

    def test_genesis_utxo_set_is_empty_and_heights_grow(self):
        self.assertEqual(self.store.block_state(GENESIS_ID), (0, set()))
        self.assertEqual(self.store.block_state(self.b1_id), (1, {(self.cb1_id, 0)}))
        self.assertIsNone(self.store.block_state("ab" * 32))

    def test_utxo_sets_are_kept_per_block(self):
        tx_a = self.add_tx(self.spend(self.cb1_id, self.alice, [(self.bob_pub, R)]))
        b2_id = self.add_block(self.block([tx_a]))
        self.assertEqual(self.store.block_state(b2_id)[1], {(tx_a, 0)})
        self.assertEqual(self.store.block_state(self.b1_id)[1], {(self.cb1_id, 0)})  # unchanged


if __name__ == "__main__":
    unittest.main()
