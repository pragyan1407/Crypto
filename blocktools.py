"""Helpers to build and mine blocks (for tests, and later for a miner)."""

import hashlib
import time

from objects import BLOCK_TARGET, object_id
from protocol import canonical_json

_TARGET_BYTES = bytes.fromhex(BLOCK_TARGET)
_PLACEHOLDER = "0" * 64


def make_block(txids, previd, created=None, miner=None, note=None, nonce=_PLACEHOLDER, target=BLOCK_TARGET):
    """An (unmined) block object. `created` defaults to the current time."""
    block = {
        "type": "block",
        "txids": list(txids),
        "nonce": nonce,
        "previd": previd,
        "created": int(time.time()) if created is None else created,
        "T": target,
    }
    if miner is not None:
        block["miner"] = miner
    if note is not None:
        block["note"] = note
    return block


def mine(block, start=0):
    """Find a nonce so that blockid < T, set it in `block` and return the block.

    Takes about 100,000 hash attempts on average (well under a second).
    """
    block["nonce"] = _PLACEHOLDER
    encoded = canonical_json(block)
    marker = b'"nonce":"'
    offset = encoded.index(marker) + len(marker)
    prefix = hashlib.blake2s(encoded[:offset])
    suffix = encoded[offset + 64 :]

    nonce = start
    while True:
        nonce_hex = f"{nonce:064x}".encode()
        hasher = prefix.copy()
        hasher.update(nonce_hex + suffix)
        if hasher.digest() < _TARGET_BYTES:
            block["nonce"] = nonce_hex.decode()
            return block
        nonce += 1


def make_unmined(block, start=0):
    """Return `block` with a nonce that does NOT satisfy the proof of work (for negative tests)."""
    nonce = start
    while True:
        block["nonce"] = f"{nonce:064x}"
        if object_id(block) >= BLOCK_TARGET:
            return block
        nonce += 1


def mined_block(txids, previd, **kwargs):
    """make_block + mine in one call."""
    return mine(make_block(txids, previd, **kwargs))
