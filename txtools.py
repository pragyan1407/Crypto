"""Helpers to create and sign transactions (for tests, and later for mining/wallet code)."""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from objects import object_id, signing_payload


def new_private_key():
    return Ed25519PrivateKey.generate()


def pubkey_hex(private_key):
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return raw.hex()


def make_coinbase(pubkey, value=50 * 10**12, height=0):
    return {"type": "transaction", "height": height, "outputs": [{"pubkey": pubkey, "value": value}]}


def make_tx(spends, outputs):
    """Build and sign a transaction.

    spends:  list of (txid, index, private_key) - the key must own the spent output
    outputs: list of (pubkey_hex, value)
    """
    tx = {
        "type": "transaction",
        "inputs": [{"outpoint": {"txid": txid, "index": index}, "sig": None} for txid, index, _ in spends],
        "outputs": [{"pubkey": pubkey, "value": value} for pubkey, value in outputs],
    }
    payload = signing_payload(tx)
    for inp, (_, _, key) in zip(tx["inputs"], spends):
        inp["sig"] = key.sign(payload).hex()
    return tx


def txid(tx):
    return object_id(tx)
