"""Application objects: ids, the genesis block and transaction syntax checks."""

import hashlib

from protocol import INVALID_FORMAT, ProtocolError, canonical_json, is_hex, is_printable_ascii

GENESIS = {
    "T": "0000abc000000000000000000000000000000000000000000000000000000000",
    "created": 1671062400,
    "miner": "Marabu",
    "nonce": "00000000000000000000000000000000000000000000000000000000005bb0f2",
    "note": "The New York Times 2022-12-13: Scientists Achieve Nuclear Fusion Breakthrough With Blast of 192 Lasers",
    "previd": None,
    "txids": [],
    "type": "block",
}
GENESIS_ID = "00002fa163c7dab0991544424b9fd302bb1782b185e5a3bbdf12afb758e57dee"

BLOCK_TARGET = "0000abc000000000000000000000000000000000000000000000000000000000"
BLOCK_REWARD = 50 * 10**12  # picaker created by every block
MAX_TEXT_LENGTH = 128  # miner and note

PUBKEY_BYTES = 32
SIGNATURE_BYTES = 64
TXID_BYTES = 32


def object_id(obj):
    """objectid = blake2s of the canonical JSON representation."""
    return hashlib.blake2s(canonical_json(obj)).hexdigest()


def signing_payload(tx):
    """The bytes that are signed: the transaction with every `sig` replaced by null."""
    payload = dict(tx)
    payload["inputs"] = [{**inp, "sig": None} for inp in tx["inputs"]]
    return canonical_json(payload)


# --------------------------------------------------------------------------
# Syntax checks (everything that can be decided without the object database)
# --------------------------------------------------------------------------


def _is_int(value):
    # bool is a subclass of int in Python; floats such as 5.0 are not integers here either.
    return isinstance(value, int) and not isinstance(value, bool)


def _require_keys(value, keys, what):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ProtocolError(INVALID_FORMAT, f"{what} must be an object with exactly the keys {sorted(keys)}")


def _check_outputs(outputs):
    if not isinstance(outputs, list):
        raise ProtocolError(INVALID_FORMAT, "outputs must be an array")
    for output in outputs:
        _require_keys(output, {"pubkey", "value"}, "an output")
        if not is_hex(output["pubkey"], PUBKEY_BYTES):
            raise ProtocolError(INVALID_FORMAT, "output pubkey must be 64 lowercase hex characters")
        if not _is_int(output["value"]) or output["value"] < 0:
            raise ProtocolError(INVALID_FORMAT, "output value must be a non-negative integer")


def check_transaction_syntax(tx):
    """Check the structure of a transaction object. Returns "coinbase" or "normal".

    Raises ProtocolError(INVALID_FORMAT) if the structure is wrong.
    """
    if not isinstance(tx, dict):
        raise ProtocolError(INVALID_FORMAT, "transaction must be a JSON object")

    if "inputs" not in tx:  # coinbase transaction
        _require_keys(tx, {"type", "height", "outputs"}, "a coinbase transaction")
        if not _is_int(tx["height"]) or tx["height"] < 0:
            raise ProtocolError(INVALID_FORMAT, "coinbase height must be a non-negative integer")
        _check_outputs(tx["outputs"])
        if len(tx["outputs"]) != 1:
            raise ProtocolError(INVALID_FORMAT, "a coinbase transaction has exactly one output")
        return "coinbase"

    _require_keys(tx, {"type", "inputs", "outputs"}, "a transaction")
    inputs = tx["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise ProtocolError(INVALID_FORMAT, "inputs must be a non-empty array")
    for inp in inputs:
        _require_keys(inp, {"outpoint", "sig"}, "an input")
        outpoint = inp["outpoint"]
        _require_keys(outpoint, {"txid", "index"}, "an outpoint")
        if not is_hex(outpoint["txid"], TXID_BYTES):
            raise ProtocolError(INVALID_FORMAT, "outpoint txid must be 64 lowercase hex characters")
        if not _is_int(outpoint["index"]) or outpoint["index"] < 0:
            raise ProtocolError(INVALID_FORMAT, "outpoint index must be a non-negative integer")
        if not is_hex(inp["sig"], SIGNATURE_BYTES):
            raise ProtocolError(INVALID_FORMAT, "sig must be 128 lowercase hex characters")
    _check_outputs(tx["outputs"])
    return "normal"


def check_block_syntax(block):
    """Check the structure of a block object. Raises ProtocolError(INVALID_FORMAT) if it is wrong."""
    required = {"type", "txids", "nonce", "previd", "created", "T"}
    optional = {"miner", "note"}
    if not isinstance(block, dict):
        raise ProtocolError(INVALID_FORMAT, "block must be a JSON object")
    keys = set(block)
    if not required <= keys or not keys <= required | optional:
        raise ProtocolError(INVALID_FORMAT, "block has missing or additional keys")

    txids = block["txids"]
    if not isinstance(txids, list) or not all(is_hex(t, TXID_BYTES) for t in txids):
        raise ProtocolError(INVALID_FORMAT, "txids must be an array of 64 lowercase hex characters each")
    if not is_hex(block["nonce"], 32):
        raise ProtocolError(INVALID_FORMAT, "nonce must be 64 lowercase hex characters")
    if block["previd"] is not None and not is_hex(block["previd"], 32):
        raise ProtocolError(INVALID_FORMAT, "previd must be null or 64 lowercase hex characters")
    if not _is_int(block["created"]) or block["created"] < 0:
        raise ProtocolError(INVALID_FORMAT, "created must be a non-negative integer")
    if block["T"] != BLOCK_TARGET:
        raise ProtocolError(INVALID_FORMAT, "block has the wrong target")
    for key in optional & keys:
        text = block[key]
        if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH or not is_printable_ascii(text):
            raise ProtocolError(INVALID_FORMAT, f"{key} must be printable ASCII, at most 128 characters")
