"""Object, transaction and block validation (Tasks 2 and 3).

Every function raises ProtocolError with the protocol's error name if the
object is invalid. They need an ObjectStore to look up the objects an object
refers to (spent transactions, parent block, UTXO sets).

Blocks are validated in two stages because their transactions may have to be
fetched from a peer first:

  1. validate_block_header: everything that can be decided without the
     transactions. Returns the txids we do not have yet.
  2. validate_block_body: once all transactions are stored, execute them
     against the parent's UTXO set. Returns the block's height and new UTXO set.
"""

import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from objects import (
    BLOCK_REWARD,
    BLOCK_TARGET,
    check_block_syntax,
    check_transaction_syntax,
    object_id,
    signing_payload,
)
from protocol import (
    INVALID_BLOCK_COINBASE,
    INVALID_BLOCK_POW,
    INVALID_BLOCK_TIMESTAMP,
    INVALID_FORMAT,
    INVALID_GENESIS,
    INVALID_TX_CONSERVATION,
    INVALID_TX_OUTPOINT,
    INVALID_TX_SIGNATURE,
    UNKNOWN_OBJECT,
    ProtocolError,
)


def verify_signature(pubkey_hex, sig_hex, message):
    """Ed25519 verification; any malformed key or signature simply counts as invalid."""
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(bytes.fromhex(sig_hex), message)
    except (InvalidSignature, ValueError):
        return False
    return True


def validate_object(obj, store):
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError(INVALID_FORMAT, "object has no string 'type' key")
    if obj["type"] != "transaction":
        # Blocks need the two-stage validation below (the node drives it), so they never come through here.
        raise ProtocolError(INVALID_FORMAT, "unsupported object type")
    validate_transaction(obj, store)


def validate_transaction(tx, store):
    if check_transaction_syntax(tx) == "coinbase":
        return  # coinbase value and height are checked together with blocks (Task 3)

    outpoints = [(inp["outpoint"]["txid"], inp["outpoint"]["index"]) for inp in tx["inputs"]]
    if len(set(outpoints)) != len(outpoints):
        raise ProtocolError(INVALID_TX_CONSERVATION, "transaction spends the same output twice")

    # a) outpoints: every referenced transaction must be known and have that output.
    #    Definitive errors win over "unknown", which is only reported at the end.
    unknown = False
    spent_outputs = []
    for txid, index in outpoints:
        referenced = store.get(txid)
        if referenced is None:
            unknown = True
            continue
        if referenced.get("type") != "transaction":
            raise ProtocolError(INVALID_FORMAT, "an input references an object that is not a transaction")
        if index >= len(referenced["outputs"]):
            raise ProtocolError(INVALID_TX_OUTPOINT, "an input references an output that does not exist")
        spent_outputs.append(referenced["outputs"][index])
    if unknown:
        raise ProtocolError(UNKNOWN_OBJECT, "a referenced transaction is not known")

    # b) signatures, made with the key of the output that is being spent
    payload = signing_payload(tx)
    for inp, spent in zip(tx["inputs"], spent_outputs):
        if not verify_signature(spent["pubkey"], inp["sig"], payload):
            raise ProtocolError(INVALID_TX_SIGNATURE, "an input has an invalid signature")

    # d) weak law of conservation: inputs must cover outputs (the rest is the fee)
    if sum(o["value"] for o in spent_outputs) < sum(o["value"] for o in tx["outputs"]):
        raise ProtocolError(INVALID_TX_CONSERVATION, "outputs are worth more than inputs")


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def validate_block_header(block, store, now=None):
    """Stage 1: checks that do not need the block's transactions.

    Returns the list of txids (without duplicates, in block order) that are not
    in `store` yet and have to be fetched before stage 2 can run.
    """
    check_block_syntax(block)  # keys, formats and the fixed target (INVALID_FORMAT)

    if block["previd"] is None:
        # The real genesis block is preloaded in every store and is never validated again.
        raise ProtocolError(INVALID_GENESIS, "only the genesis block may have previd null")

    if int(object_id(block), 16) >= int(BLOCK_TARGET, 16):
        raise ProtocolError(INVALID_BLOCK_POW, "block does not meet the mining target")

    parent = store.get(block["previd"])
    if parent is None:
        raise ProtocolError(UNKNOWN_OBJECT, "parent block is not known")
    if parent.get("type") != "block":
        raise ProtocolError(INVALID_FORMAT, "previd references an object that is not a block")

    now = time.time() if now is None else now
    if not parent["created"] < block["created"] <= now:
        raise ProtocolError(
            INVALID_BLOCK_TIMESTAMP, "created must be later than the parent's timestamp and not in the future"
        )

    return [txid for txid in dict.fromkeys(block["txids"]) if not store.has(txid)]


def _output_value(store, txid, index):
    return store.get(txid)["outputs"][index]["value"]


def validate_block_body(block, store):
    """Stage 2: execute the block's transactions. All of them must be in `store`.

    Returns (height, utxo) where utxo is the set of unspent (txid, index) pairs
    after the block. Nothing is stored here.
    """
    parent_state = store.block_state(block["previd"])
    if parent_state is None:
        raise ProtocolError(UNKNOWN_OBJECT, "parent block is not known")
    parent_height, parent_utxo = parent_state
    height = parent_height + 1
    utxo = set(parent_utxo)

    coinbase_id = None
    fees = 0
    for position, txid in enumerate(block["txids"]):
        tx = store.get(txid)
        if tx is None:
            raise ProtocolError(UNKNOWN_OBJECT, f"transaction {txid} is not known")
        if tx.get("type") != "transaction":
            raise ProtocolError(INVALID_FORMAT, "txids references an object that is not a transaction")

        if "inputs" not in tx:  # coinbase transaction
            if position != 0:
                raise ProtocolError(
                    INVALID_BLOCK_COINBASE, "a coinbase transaction must be first, and there can only be one"
                )
            if tx["height"] != height:
                raise ProtocolError(INVALID_BLOCK_COINBASE, "coinbase height does not match the block height")
            coinbase_id = txid
            continue

        # Ordinary transaction. Its syntax, signatures and conservation were verified when it was stored
        # (Task 2); what depends on the chain is whether every input is still unspent.
        total_in = 0
        for inp in tx["inputs"]:
            outpoint = (inp["outpoint"]["txid"], inp["outpoint"]["index"])
            if outpoint not in utxo:
                # unknown, already spent (also within this block) or the coinbase of this very block,
                # whose output only becomes spendable once the block is complete
                raise ProtocolError(INVALID_TX_OUTPOINT, "an input spends an output that is not unspent")
            utxo.remove(outpoint)
            total_in += _output_value(store, *outpoint)
        total_out = sum(output["value"] for output in tx["outputs"])
        if total_out > total_in:  # cannot happen for stored transactions; kept as a safety net
            raise ProtocolError(INVALID_TX_CONSERVATION, "outputs are worth more than inputs")
        fees += total_in - total_out
        utxo.update((txid, index) for index in range(len(tx["outputs"])))

    if coinbase_id is not None:
        value = _output_value(store, coinbase_id, 0)
        if value > BLOCK_REWARD + fees:
            raise ProtocolError(
                INVALID_BLOCK_COINBASE, "coinbase creates more than the block reward plus the fees"
            )
        utxo.add((coinbase_id, 0))

    return height, utxo
