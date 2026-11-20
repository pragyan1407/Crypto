import hashlib
import unittest

from protocol import (
    INVALID_FORMAT,
    ProtocolError,
    canonical_json,
    is_valid_peer_address,
    parse_json_object,
    validate_getpeers,
    validate_hello,
    validate_peers,
)

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


class CanonicalJsonTest(unittest.TestCase):
    def test_sorted_and_compact(self):
        self.assertEqual(canonical_json({"b": 1, "a": [1, 2], "c": None}), b'{"a":[1,2],"b":1,"c":null}')

    def test_genesis_block_id(self):
        # Sanity check for canonical JSON + blake2s (needed in Task 2).
        self.assertEqual(hashlib.blake2s(canonical_json(GENESIS)).hexdigest(), GENESIS_ID)


class ParseTest(unittest.TestCase):
    def assertInvalid(self, line):
        with self.assertRaises(ProtocolError) as ctx:
            parse_json_object(line)
        self.assertEqual(ctx.exception.name, INVALID_FORMAT)

    def test_valid_non_canonical(self):
        msg = parse_json_object(b'  { "type" :  "getpeers" }\n')
        self.assertEqual(msg, {"type": "getpeers"})

    def test_invalid_inputs(self):
        for line in [
            b"Wbgygvf7rgtyv7tfbgy{{{\n",
            b'"type":"diufygeuybhv"\n',
            b"\n",
            b"[]\n",
            b'"hello"\n',
            b"{}\n",
            b'{"type": 5}\n',
            b'{"type": NaN}\n',
            b'{"type":"a","type":"b"}\n',
            b'{"type":"getpeers"\n',
            b"\xff\xfe\n",
            b"[" * 100000 + b"\n",
        ]:
            with self.subTest(line=line[:30]):
                self.assertInvalid(line)


class HelloTest(unittest.TestCase):
    def hello(self, **overrides):
        msg = {"type": "hello", "version": "0.10.0", "agent": "Test Client 1.0"}
        msg.update(overrides)
        return msg

    def test_valid(self):
        validate_hello(self.hello())
        validate_hello(self.hello(version="0.10.9"))
        validate_hello(self.hello(agent=""))
        validate_hello(self.hello(agent="a" * 128))

    def test_invalid(self):
        bad = [
            {"type": "hello"},
            {"type": "hello", "version": "jd3.x"},
            {"type": "hello", "version": "5.8.2"},
            self.hello(version="0.10.12"),
            self.hello(version="0.10"),
            self.hello(version=10),
            self.hello(agent="a" * 129),
            self.hello(agent="tab\there"),
            self.hello(agent="caf\u00e9"),
            self.hello(agent=None),
            {**self.hello(), "extra": 1},
        ]
        for msg in bad:
            with self.subTest(msg=msg):
                with self.assertRaises(ProtocolError) as ctx:
                    validate_hello(msg)
                self.assertEqual(ctx.exception.name, INVALID_FORMAT)


class PeersTest(unittest.TestCase):
    def test_valid_addresses(self):
        for addr in [
            "kermanode.net:18017",
            "138.197.191.170:18018",
            "128.130.122.101:18018",
            "a.b:1",
            "x.y-z_1.org:65535",
        ]:
            with self.subTest(addr=addr):
                self.assertTrue(is_valid_peer_address(addr))

    def test_invalid_addresses(self):
        for addr in [
            "256.2.3.4:18018",
            "1.2.3.4.5:678",
            "1.2.3.4:2000000",
            "192.168.0.2:18018",
            "10.0.0.1:18018",
            "127.0.0.1:18018",
            "nodotindomain:1234",
            "kermanode.net",
            "a.b:0",
            ".ab.c:80",
            "ab.c.:80",
            "ab:80",
            "a" * 49 + ".b:80",
            "bad host.net:80",
            "1.2.3.4:",
            ":80",
            "a.b.c:80:90",
            "300.300.300.300:80",
            "01.2.3.4:80",
            "a.b:+80",
            42,
            None,
        ]:
            with self.subTest(addr=addr):
                self.assertFalse(is_valid_peer_address(addr))

    def test_validate_peers_message(self):
        self.assertEqual(validate_peers({"type": "peers", "peers": []}), [])
        self.assertEqual(
            validate_peers({"type": "peers", "peers": ["kermanode.net:18018"]}), ["kermanode.net:18018"]
        )

    def test_validate_peers_message_invalid(self):
        bad = [
            {"type": "peers"},
            {"type": "peers", "peers": "kermanode.net:18018"},
            {"type": "peers", "peers": ["kermanode.net:18018", "nodotindomain:1234"]},
            {"type": "peers", "peers": ["kermanode.net:18018"] * 31},
            {"type": "peers", "peers": [], "extra": 1},
        ]
        for msg in bad:
            with self.subTest(msg=str(msg)[:40]):
                with self.assertRaises(ProtocolError):
                    validate_peers(msg)

    def test_thirty_peers_ok(self):
        validate_peers({"type": "peers", "peers": ["kermanode.net:18018"] * 30})

    def test_getpeers_extra_key(self):
        validate_getpeers({"type": "getpeers"})
        with self.assertRaises(ProtocolError):
            validate_getpeers({"type": "getpeers", "x": 1})


if __name__ == "__main__":
    unittest.main()
