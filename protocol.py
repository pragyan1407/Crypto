"""Kerma wire protocol helpers (Task 1).

Canonical JSON encoding, message parsing/validation and peer-address
validation. Nothing in here does any I/O, so it is easy to unit test.
"""

import ipaddress
import json
import re

PROTOCOL_VERSION = "0.10.0"
MAX_PEERS_PER_MESSAGE = 30
MAX_AGENT_LENGTH = 128


INVALID_FORMAT = "INVALID_FORMAT"
INVALID_HANDSHAKE = "INVALID_HANDSHAKE"
INVALID_TX_CONSERVATION = "INVALID_TX_CONSERVATION"
INVALID_TX_SIGNATURE = "INVALID_TX_SIGNATURE"
INVALID_TX_OUTPOINT = "INVALID_TX_OUTPOINT"
INVALID_BLOCK_POW = "INVALID_BLOCK_POW"
INVALID_BLOCK_TIMESTAMP = "INVALID_BLOCK_TIMESTAMP"
INVALID_BLOCK_COINBASE = "INVALID_BLOCK_COINBASE"
INVALID_GENESIS = "INVALID_GENESIS"
UNKNOWN_OBJECT = "UNKNOWN_OBJECT"
UNFINDABLE_OBJECT = "UNFINDABLE_OBJECT"
INVALID_ANCESTRY = "INVALID_ANCESTRY"

# Errors we report but that do not mean the peer is faulty: the connection stays open.
NON_FATAL_ERRORS = frozenset({UNKNOWN_OBJECT, UNFINDABLE_OBJECT, INVALID_ANCESTRY})

# Valid message types that we do not act on yet (later tasks).
IGNORED_TYPES = frozenset(
    {
        "getchaintip",
        "chaintip",
        "getmempool",
        "mempool",
        "error",
    }
)

_VERSION_RE = re.compile(r"0\.10\.[0-9]", re.ASCII)
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}", re.ASCII)
_IPV4_RE = re.compile(r"(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}", re.ASCII)
_DNS_RE = re.compile(r"[a-zA-Z0-9.\-_]{3,50}", re.ASCII)
_HEX_RE = re.compile(r"[0-9a-f]*", re.ASCII)


class ProtocolError(Exception):
    """Raised when a peer violates the protocol. `name` is the Kerma error name."""

    def __init__(self, name, msg):
        super().__init__(f"{name}: {msg}")
        self.name = name
        self.msg = msg


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def canonical_json(obj):
    """Canonical JSON as bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def make_hello(agent):
    return {"type": "hello", "version": PROTOCOL_VERSION, "agent": agent}


def make_getpeers():
    return {"type": "getpeers"}


def make_peers(peers):
    return {"type": "peers", "peers": list(peers)}


def make_getobject(objectid):
    return {"type": "getobject", "objectid": objectid}


def make_ihaveobject(objectid):
    return {"type": "ihaveobject", "objectid": objectid}


def make_object(obj):
    return {"type": "object", "object": obj}


def make_error(name, msg):
    return {"type": "error", "name": name, "msg": msg}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse_json_object(line):
    """Decode one received line into a dict that has a string `type`.

    Raises ProtocolError(INVALID_FORMAT) for anything else. The input does not
    need to be in canonical form.
    """
    try:
        obj = json.loads(
            line.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except (ValueError, RecursionError):
        raise ProtocolError(INVALID_FORMAT, "message is not valid JSON") from None
    if not isinstance(obj, dict):
        raise ProtocolError(INVALID_FORMAT, "message is not a JSON object")
    if not isinstance(obj.get("type"), str):
        raise ProtocolError(INVALID_FORMAT, "message has no string 'type' key")
    return obj


# --------------------------------------------------------------------------
# Message validation
# --------------------------------------------------------------------------


def is_printable_ascii(text):
    return all(0x20 <= ord(c) <= 0x7E for c in text)


def validate_hello(msg):
    if set(msg) != {"type", "version", "agent"}:
        raise ProtocolError(INVALID_FORMAT, "hello must contain exactly type, version and agent")
    version, agent = msg["version"], msg["agent"]
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ProtocolError(INVALID_FORMAT, "unsupported version, expected 0.10.x")
    if not isinstance(agent, str) or len(agent) > MAX_AGENT_LENGTH or not is_printable_ascii(agent):
        raise ProtocolError(INVALID_FORMAT, "agent must be printable ASCII, at most 128 characters")


def validate_getpeers(msg):
    if set(msg) != {"type"}:
        raise ProtocolError(INVALID_FORMAT, "getpeers must not contain additional keys")


def validate_peers(msg):
    """Validate a peers message and return the list of peer address strings."""
    if set(msg) != {"type", "peers"}:
        raise ProtocolError(INVALID_FORMAT, "peers must contain exactly type and peers")
    peers = msg["peers"]
    if not isinstance(peers, list):
        raise ProtocolError(INVALID_FORMAT, "peers must be an array")
    if len(peers) > MAX_PEERS_PER_MESSAGE:
        raise ProtocolError(INVALID_FORMAT, "peers contains more than 30 entries")
    for entry in peers:
        try:
            split_peer_address(entry)
        except ValueError:
            raise ProtocolError(INVALID_FORMAT, "peers contains an invalid peer address") from None
    return list(peers)


def is_hex(value, nbytes):
    """True if value is a lowercase hex string encoding exactly `nbytes` bytes."""
    return isinstance(value, str) and len(value) == 2 * nbytes and _HEX_RE.fullmatch(value) is not None


def _validate_objectid_message(msg, what):
    if set(msg) != {"type", "objectid"}:
        raise ProtocolError(INVALID_FORMAT, f"{what} must contain exactly type and objectid")
    if not is_hex(msg["objectid"], 32):
        raise ProtocolError(INVALID_FORMAT, "objectid must be 64 lowercase hex characters")
    return msg["objectid"]


def validate_getobject(msg):
    """Validate a getobject message and return the requested objectid."""
    return _validate_objectid_message(msg, "getobject")


def validate_ihaveobject(msg):
    """Validate an ihaveobject message and return the advertised objectid."""
    return _validate_objectid_message(msg, "ihaveobject")


def validate_object_message(msg):
    """Validate the envelope of an object message and return the contained object."""
    if set(msg) != {"type", "object"}:
        raise ProtocolError(INVALID_FORMAT, "object message must contain exactly type and object")
    if not isinstance(msg["object"], dict):
        raise ProtocolError(INVALID_FORMAT, "object must be a JSON object")
    return msg["object"]


# --------------------------------------------------------------------------
# Peer addresses
# --------------------------------------------------------------------------


def split_peer_address(addr):
    """Validate a `host:port` string and return (host, port).

    host: valid DNS name (per the protocol rules) or public IPv4 address.
    Raises ValueError if the address is not syntactically valid.
    """
    if not isinstance(addr, str) or addr.count(":") != 1:
        raise ValueError("expected host:port")
    host, port_text = addr.split(":")
    if not _PORT_RE.fullmatch(port_text) or int(port_text) > 65535:
        raise ValueError("invalid port")

    if _IPV4_RE.fullmatch(host):
        octets = [int(part) for part in host.split(".")]
        if any(o > 255 for o in octets):
            raise ValueError("invalid IPv4 address")
        ip = ipaddress.IPv4Address(host)
        if not ip.is_global or ip.is_multicast:
            raise ValueError("IPv4 address is not public")
    else:
        if not _DNS_RE.fullmatch(host):
            raise ValueError("invalid host name")
        if "." not in host[1:-1]:
            raise ValueError("host name needs a dot that is not first or last")
        if not any(c.isalpha() for c in host):
            raise ValueError("host name needs at least one letter")
    return host, int(port_text)


def is_valid_peer_address(addr):
    try:
        split_peer_address(addr)
    except ValueError:
        return False
    return True
