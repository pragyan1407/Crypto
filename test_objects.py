import copy
import unittest

from fixtures import PDF_COINBASE, PDF_COINBASE_ID, PDF_TX, PDF_TX_ID
from objects import GENESIS, GENESIS_ID, object_id, signing_payload
from objectstore import ObjectStore
from protocol import (
    INVALID_FORMAT,
    INVALID_TX_CONSERVATION,
    INVALID_TX_OUTPOINT,
    INVALID_TX_SIGNATURE,
    UNKNOWN_OBJECT,
    ProtocolError,
    validate_getobject,
    validate_ihaveobject,
    validate_object_message,
)
from txtools import make_coinbase, make_tx, new_private_key, pubkey_hex, txid
from validation import validate_object


class ObjectIdTest(unittest.TestCase):
    def test_genesis(self):
        self.assertEqual(object_id(GENESIS), GENESIS_ID)

    def test_pdf_examples(self):
        self.assertEqual(object_id(PDF_COINBASE), PDF_COINBASE_ID)
        self.assertEqual(object_id(PDF_TX), PDF_TX_ID)

    def test_key_order_does_not_matter(self):
        reordered = dict(reversed(list(PDF_COINBASE.items())))
        self.assertEqual(object_id(reordered), PDF_COINBASE_ID)


class ObjectStoreTest(unittest.TestCase):
    def test_genesis_is_preloaded_and_nothing_else(self):
        store = ObjectStore(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(store.ids(), [GENESIS_ID])
        self.assertEqual(store.get(GENESIS_ID), GENESIS)

    def test_put_get_has(self):
        store = ObjectStore(":memory:")
        self.addCleanup(store.close)
        self.assertFalse(store.has(PDF_COINBASE_ID))
        self.assertIsNone(store.get(PDF_COINBASE_ID))
        self.assertTrue(store.put(PDF_COINBASE_ID, PDF_COINBASE))
        self.assertFalse(store.put(PDF_COINBASE_ID, PDF_COINBASE))  # already there
        self.assertTrue(store.has(PDF_COINBASE_ID))
        self.assertEqual(store.get(PDF_COINBASE_ID), PDF_COINBASE)

    def test_survives_reopening(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "objects.db")
            store = ObjectStore(path)
            store.put(PDF_COINBASE_ID, PDF_COINBASE)
            store.close()
            reopened = ObjectStore(path)
            self.assertEqual(reopened.get(PDF_COINBASE_ID), PDF_COINBASE)
            self.assertEqual(len(reopened), 2)
            reopened.close()


class MessageValidationTest(unittest.TestCase):
    def test_objectid_messages(self):
        self.assertEqual(validate_getobject({"type": "getobject", "objectid": GENESIS_ID}), GENESIS_ID)
        self.assertEqual(validate_ihaveobject({"type": "ihaveobject", "objectid": GENESIS_ID}), GENESIS_ID)

    def test_bad_objectid_messages(self):
        for bad in [
            {"type": "getobject"},
            {"type": "getobject", "objectid": GENESIS_ID, "x": 1},
            {"type": "getobject", "objectid": GENESIS_ID.upper()},
            {"type": "getobject", "objectid": GENESIS_ID[:-1]},
            {"type": "getobject", "objectid": "zz" * 32},
            {"type": "getobject", "objectid": 5},
            {"type": "getobject", "objectid": None},
        ]:
            with self.subTest(msg=bad):
                with self.assertRaises(ProtocolError) as ctx:
                    validate_getobject(bad)
                self.assertEqual(ctx.exception.name, INVALID_FORMAT)

    def test_object_message(self):
        self.assertEqual(validate_object_message({"type": "object", "object": {"a": 1}}), {"a": 1})
        for bad in [{"type": "object"}, {"type": "object", "object": []}, {"type": "object", "object": 1}, {"type": "object", "object": {}, "x": 1}]:
            with self.subTest(msg=bad):
                with self.assertRaises(ProtocolError):
                    validate_object_message(bad)


class TransactionValidationTest(unittest.TestCase):
    def setUp(self):
        self.store = ObjectStore(":memory:")
        self.addCleanup(self.store.close)
        self.alice = new_private_key()
        self.bob = new_private_key()
        self.alice_pub = pubkey_hex(self.alice)
        self.bob_pub = pubkey_hex(self.bob)
        self.coinbase = make_coinbase(self.alice_pub, value=1000)
        self.cb_id = txid(self.coinbase)
        self.store.put(self.cb_id, self.coinbase)

    def spend(self, outputs=None, index=0, key=None, txid_=None):
        return make_tx(
            [(txid_ or self.cb_id, index, key or self.alice)],
            outputs if outputs is not None else [(self.bob_pub, 1000)],
        )

    def assertInvalid(self, tx, name):
        with self.assertRaises(ProtocolError) as ctx:
            validate_object(tx, self.store)
        self.assertEqual(ctx.exception.name, name, ctx.exception.msg)

    # -- valid ----------------------------------------------------------------

    def test_pdf_examples_are_valid(self):
        store = ObjectStore(":memory:")
        self.addCleanup(store.close)
        store.put(PDF_COINBASE_ID, PDF_COINBASE)
        validate_object(PDF_COINBASE, store)
        validate_object(PDF_TX, store)

    def test_valid_transactions(self):
        validate_object(self.spend(), self.store)
        validate_object(self.spend(outputs=[(self.bob_pub, 600), (self.alice_pub, 399)]), self.store)  # fee of 1
        validate_object(self.spend(outputs=[]), self.store)  # burn everything as fee
        validate_object(self.spend(outputs=[(self.bob_pub, 0)]), self.store)  # zero value output
        validate_object(make_coinbase(self.bob_pub, height=7, value=123), self.store)

    def test_chain_of_spends_and_several_inputs(self):
        first = self.spend(outputs=[(self.bob_pub, 400), (self.alice_pub, 600)])
        first_id = txid(first)
        validate_object(first, self.store)
        self.store.put(first_id, first)
        second = make_tx([(first_id, 0, self.alice), (first_id, 1, self.alice)], [(self.bob_pub, 1000)])
        self.assertInvalid(second, INVALID_TX_SIGNATURE)  # output 0 belongs to bob, not alice
        second = make_tx([(first_id, 0, self.bob), (first_id, 1, self.alice)], [(self.bob_pub, 1000)])
        validate_object(second, self.store)

    def test_same_output_may_be_spent_by_different_transactions(self):
        validate_object(self.spend(outputs=[(self.bob_pub, 1)]), self.store)
        validate_object(self.spend(outputs=[(self.bob_pub, 2)]), self.store)

    # -- semantic errors ------------------------------------------------------

    def test_unknown_input_transaction(self):
        self.assertInvalid(self.spend(txid_="ab" * 32), UNKNOWN_OBJECT)

    def test_input_references_block(self):
        self.assertInvalid(self.spend(txid_=GENESIS_ID), INVALID_FORMAT)

    def test_outpoint_index_too_high(self):
        self.assertInvalid(self.spend(index=1), INVALID_TX_OUTPOINT)
        self.assertInvalid(self.spend(index=10**9), INVALID_TX_OUTPOINT)

    def test_definitive_error_wins_over_unknown(self):
        tx = make_tx([("ab" * 32, 0, self.alice), (self.cb_id, 5, self.alice)], [(self.bob_pub, 1)])
        self.assertInvalid(tx, INVALID_TX_OUTPOINT)

    def test_wrong_signing_key(self):
        self.assertInvalid(self.spend(key=self.bob), INVALID_TX_SIGNATURE)

    def test_tampered_signature(self):
        tx = self.spend()
        sig = tx["inputs"][0]["sig"]
        tx["inputs"][0]["sig"] = ("0" if sig[0] != "0" else "1") + sig[1:]
        self.assertInvalid(tx, INVALID_TX_SIGNATURE)

    def test_signature_does_not_cover_changed_outputs(self):
        tx = self.spend(outputs=[(self.bob_pub, 500)])
        tx["outputs"][0]["value"] = 501
        self.assertInvalid(tx, INVALID_TX_SIGNATURE)
        tx = self.spend()
        tx["outputs"][0]["pubkey"] = self.alice_pub
        self.assertInvalid(tx, INVALID_TX_SIGNATURE)

    def test_signature_of_other_transaction_is_rejected(self):
        other = self.spend(outputs=[(self.bob_pub, 5)])
        tx = self.spend(outputs=[(self.bob_pub, 6)])
        tx["inputs"][0]["sig"] = other["inputs"][0]["sig"]
        self.assertInvalid(tx, INVALID_TX_SIGNATURE)

    def test_outputs_exceed_inputs(self):
        self.assertInvalid(self.spend(outputs=[(self.bob_pub, 1001)]), INVALID_TX_CONSERVATION)
        self.assertInvalid(self.spend(outputs=[(self.bob_pub, 500), (self.alice_pub, 501)]), INVALID_TX_CONSERVATION)

    def test_double_spend_inside_transaction(self):
        tx = make_tx([(self.cb_id, 0, self.alice), (self.cb_id, 0, self.alice)], [(self.bob_pub, 2000)])
        self.assertInvalid(tx, INVALID_TX_CONSERVATION)
        tx = make_tx([(self.cb_id, 0, self.alice), (self.cb_id, 0, self.alice)], [(self.bob_pub, 1)])
        self.assertInvalid(tx, INVALID_TX_CONSERVATION)

    # -- syntax errors --------------------------------------------------------

    def mutated(self, mutate):
        tx = copy.deepcopy(self.spend())
        mutate(tx)
        return tx

    def test_syntax_errors_in_transactions(self):
        mutations = {
            "extra key": lambda t: t.update(extra=1),
            "missing outputs": lambda t: t.pop("outputs"),
            "missing type": lambda t: t.pop("type"),
            "inputs empty": lambda t: t.update(inputs=[]),
            "inputs not a list": lambda t: t.update(inputs={}),
            "inputs null": lambda t: t.update(inputs=None),
            "outputs not a list": lambda t: t.update(outputs="x"),
            "input extra key": lambda t: t["inputs"][0].update(extra=1),
            "input without sig": lambda t: t["inputs"][0].pop("sig"),
            "input without outpoint": lambda t: t["inputs"][0].pop("outpoint"),
            "input not an object": lambda t: t.update(inputs=[1]),
            "sig null": lambda t: t["inputs"][0].update(sig=None),
            "sig uppercase": lambda t: t["inputs"][0].update(sig=t["inputs"][0]["sig"].upper()),
            "sig too short": lambda t: t["inputs"][0].update(sig=t["inputs"][0]["sig"][:-2]),
            "sig too long": lambda t: t["inputs"][0].update(sig=t["inputs"][0]["sig"] + "00"),
            "sig not hex": lambda t: t["inputs"][0].update(sig="zz" * 64),
            "sig number": lambda t: t["inputs"][0].update(sig=5),
            "outpoint extra key": lambda t: t["inputs"][0]["outpoint"].update(extra=1),
            "outpoint without index": lambda t: t["inputs"][0]["outpoint"].pop("index"),
            "txid uppercase": lambda t: t["inputs"][0]["outpoint"].update(txid=self.cb_id.upper()),
            "txid too short": lambda t: t["inputs"][0]["outpoint"].update(txid=self.cb_id[:-2]),
            "txid number": lambda t: t["inputs"][0]["outpoint"].update(txid=1),
            "index negative": lambda t: t["inputs"][0]["outpoint"].update(index=-1),
            "index float": lambda t: t["inputs"][0]["outpoint"].update(index=0.0),
            "index bool": lambda t: t["inputs"][0]["outpoint"].update(index=False),
            "index string": lambda t: t["inputs"][0]["outpoint"].update(index="0"),
            "output extra key": lambda t: t["outputs"][0].update(extra=1),
            "output without value": lambda t: t["outputs"][0].pop("value"),
            "output not an object": lambda t: t.update(outputs=[5]),
            "value negative": lambda t: t["outputs"][0].update(value=-1),
            "value float": lambda t: t["outputs"][0].update(value=1.5),
            "value whole float": lambda t: t["outputs"][0].update(value=10.0),
            "value bool": lambda t: t["outputs"][0].update(value=True),
            "value string": lambda t: t["outputs"][0].update(value="10"),
            "value null": lambda t: t["outputs"][0].update(value=None),
            "pubkey uppercase": lambda t: t["outputs"][0].update(pubkey=self.bob_pub.upper()),
            "pubkey too short": lambda t: t["outputs"][0].update(pubkey=self.bob_pub[:-2]),
            "pubkey too long": lambda t: t["outputs"][0].update(pubkey=self.bob_pub + "00"),
            "pubkey not hex": lambda t: t["outputs"][0].update(pubkey="g" * 64),
            "pubkey number": lambda t: t["outputs"][0].update(pubkey=1),
            "coinbase fields mixed in": lambda t: t.update(height=0),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                self.assertInvalid(self.mutated(mutate), INVALID_FORMAT)

    def test_syntax_errors_in_coinbase(self):
        good = make_coinbase(self.bob_pub, value=5, height=3)
        validate_object(good, self.store)
        mutations = {
            "no height": lambda t: t.pop("height"),
            "height string": lambda t: t.update(height="3"),
            "height float": lambda t: t.update(height=3.0),
            "height bool": lambda t: t.update(height=True),
            "height negative": lambda t: t.update(height=-1),
            "extra key": lambda t: t.update(extra=1),
            "no outputs key": lambda t: t.pop("outputs"),
            "two outputs": lambda t: t["outputs"].append(dict(t["outputs"][0])),
            "no outputs": lambda t: t.update(outputs=[]),
            "negative value": lambda t: t["outputs"][0].update(value=-5),
            "bad pubkey": lambda t: t["outputs"][0].update(pubkey="abc"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                tx = copy.deepcopy(good)
                mutate(tx)
                self.assertInvalid(tx, INVALID_FORMAT)

    def test_other_object_types_and_shapes(self):
        for bad in [GENESIS, {"type": "foo"}, {"type": 1}, {}, [], "transaction", 5, None]:
            with self.subTest(obj=str(bad)[:30]):
                self.assertInvalid(bad, INVALID_FORMAT)

    def test_signing_payload_nulls_all_sigs(self):
        tx = make_tx([(self.cb_id, 0, self.alice), (self.cb_id, 1, self.alice)], [(self.bob_pub, 1)])
        self.assertEqual(signing_payload(tx).count(b'"sig":null'), 2)
        self.assertNotIn(tx["inputs"][0]["sig"].encode(), signing_payload(tx))


if __name__ == "__main__":
    unittest.main()
