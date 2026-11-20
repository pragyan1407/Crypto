"""Replay of the exact messages from a real Task 2 grader log.

The log wraps long lines by inserting spaces, so all spaces are stripped before
sending (no legitimate value contains one). The ids the grader printed
(51f5c3dd..., 5d717a7c...) must be reproduced by our own object_id(), and the
signature in the "valid" transaction must verify: both prove that our canonical
JSON, blake2s hashing and Ed25519 handling match the grader's.

State carries over between the steps, exactly as in the grader run: the invalid
transactions refer to the transaction stored earlier in the sequence.
"""

import asyncio
import json

from objects import object_id
from test_node import NodeTestCase


def raw(text):
    return text.replace(" ", "").encode() + b"\n"


COINBASE = raw('{"object":{"height":0,"outputs":[{"pubkey":"e2c358e003cd5191e19bd7f01974de24a2c4ded3d fbf4fc4682a63cc1bdcfa14","value":50000000000}],"type":"transaction"},"type":"object"}')
COINBASE_ID = "51f5c3dd436b6ed8dc8f44858ad0b2cc0d8cc866140a7ed9f1158e6840a76d97"

VALID_TX = raw('{"object":{"inputs":[{"outpoint":{"index":0,"txid":"51f5c3dd436b6ed8dc8f44858ad0b2cc0d8cc866140a7ed 9f1158e6840a76d97"},"sig":"c27451223471222a9ab90f3da3d8618ebaef96c00b90161bc4e5fc95f49dc2e4efac40d6 7dff7a6b8c04eb565623eac5b848a24648ec68bdaad03226a42b0005"}],"outputs":[{"pubkey":"e2c358e003cd5191e 19bd7f01974de24a2c4ded3dfbf4fc4682a63cc1bdcfa14","value":10}],"type":"transaction"},"type":"object"}')
VALID_TX_ID = "5d717a7c59655243be11930374a4a7a1f26c8c7c74227ea8bbd4887d56e2bf5f"

UNKNOWN_OUTPOINT = raw('{"object":{"inputs":[{"outpoint":{"index":0,"txid":"6d717a7c59655243be11930374a4a7a1f26c8c7c74227ea 8bbd4887d56e2bf5f"},"sig":"9a427f94f3df210e2d8fea30f0d9f230a5b47aae5ed06361c273027f0905e96828fd2bab 63a8e3a10a6b8e69413f673eb83d5538a488a4524a71fbe1e76cd604"}],"outputs":[{"pubkey":"e2c358e003cd5191e 19bd7f01974de24a2c4ded3dfbf4fc4682a63cc1bdcfa14","value":10}],"type":"transaction"},"type":"object"}')

MISSING_VALUE = raw('{"object":{"inputs":[{"outpoint":{"index":0,"txid":"5d717a7c59655243be11930374a4a7a1f26c8c7c7422 7ea8bbd4887d56e2bf5f"},"sig":"0e059dbaea9affc6a35c0ef04cebdf2c726de400230500762ccaa677c8a9f5f91e7 1b8a687d9f44ba5d1f2fc456309e5f8ff5c782601afc9b525b917c9ccd600"}],"outputs":[{"pubkey":"e2c358e00 3cd5191e19bd7f01974de24a2c4ded3dfbf4fc4682a63cc1bdcfa14"}],"type":"transaction"},"type":"object"}')

BAD_OUTPUT_INDEX = raw('{"object":{"inputs":[{"outpoint":{"index":1,"txid":"5d717a7c59655243be11930374a4a7a1f26c8c7c74227ea 8bbd4887d56e2bf5f"},"sig":"a3eecf52a5a39f4397708e5a0f5cfc5b5807adb8ef9bce0078f55be29a91654f6c4de167 a06bdfc59f7832568fc5dc7b9a0f051f236ba12e6dbb9e2f02957509"}],"outputs":[{"pubkey":"e2c358e003cd5191e 19bd7f01974de24a2c4ded3dfbf4fc4682a63cc1bdcfa14","value":10}],"type":"transaction"},"type":"object"}')

SAME_OUTPOINT_TWICE = raw('{"object":{"inputs":[{"outpoint":{"index":0,"txid":"5d717a7c59655243be11930374 a4a7a1f26c8c7c74227ea8bbd4887d56e2bf5f"},"sig":"817b276a46f44b8bed20644817fb5b7 3c969fc4b0213a1ba4742f305775c6417e3aa5953c1525b65642ebcbd129786c36270c3cf3c83e e5796ffe75eb6ee5103"},{"outpoint":{"index":0,"txid":"5d717a7c59655243be11930374 a4a7a1f26c8c7c74227ea8bbd4887d56e2bf5f"},"sig":"817b276a46f44b8bed20644817fb5b 73c969fc4b0213a1ba4742f305775c6417e3aa5953c1525b65642ebcbd129786c36270c3cf3c83e e5796ffe75eb6ee5103"}],"outputs":[{"pubkey":"e2c358e003cd5191e19bd7f01974de24a 2c4ded3dfbf4fc4682a63cc1bdcfa14","value":5000000000011}],"type":"transaction"},"type":"object"}')

WRONG_SIGNATURE = raw('{"object":{"inputs":[{"outpoint":{"index":0,"txid":"5d717a7c59655243be11930374a4a7a1f26c8c7c74227ea 8bbd4887d56e2bf5f"},"sig":"a3eecf52a5a39f4397708e5a0f5cfc5b5807adb8ef9bce0078f55be29a91654f6c4de167 a06bdfc59f7832568fc5dc7b9a0f051f236ba12e6dbb9e2f02957509"}],"outputs":[{"pubkey":"e2c358e003cd5191e 19bd7f01974de24a2c4ded3dfbf4fc4682a63cc1bdcfa14","value":10}],"type":"transaction"},"type":"object"}')

NEGATIVE_VALUE = raw('{"object":{"inputs":[{"outpoint":{"index":0,"txid":"5d717a7c59655243be11930374a4a7a1f26c8c7c74227ea8bbd4887d56e2bf5f"},"sig":"63b3035a9ae77caa2969cb71a726d0c31d44d88f4c65e005ca036420171b3b d9223c5229b5e1f2bc3c7b45338b9ec8134bbaa8caaff71f73771ecee20c98b609"}], "outputs":[{"pubkey":"e2c358e003cd5191e19bd7f01974de24a2c4ded3dfbf4fc4682a63cc1bdcfa14","value":-10}],"type":"transaction"},"type":"object"}')

UNKNOWN_ID_FOR_IHAVEOBJECT = "816667cda487005c5bc6e3ba7b5c3954c27408f447d122ee1cb1ceed40a6b62e"
UNKNOWN_ID_FOR_GETOBJECT = "0015edc4d0ce20a3c0146709b782cc6dce0c41ef668544068024939f8fea8439"


class GraderTask2LogTest(NodeTestCase):
    async def silent(self, client, seconds=0.5):
        with self.assertRaises(asyncio.TimeoutError):
            await client.recv(timeout=seconds)

    async def test_ids_in_log_are_reproduced(self):
        self.assertEqual(object_id(json.loads(COINBASE)["object"]), COINBASE_ID)
        self.assertEqual(object_id(json.loads(VALID_TX)["object"]), VALID_TX_ID)

    async def test_valid_transactions_are_gossiped_and_served(self):
        g1 = await self.ready_client()
        g2 = await self.ready_client()
        for line, objectid in ((COINBASE, COINBASE_ID), (VALID_TX, VALID_TX_ID)):
            await g1.send_raw(line)
            self.assertEqual(await g2.recv(), {"objectid": objectid, "type": "ihaveobject"})
            await g2.send({"type": "getobject", "objectid": objectid})
            self.assertEqual(await g2.recv(), {"type": "object", "object": json.loads(line)["object"]})

    async def test_ihaveobject_gets_getobject(self):
        g1 = await self.ready_client()
        await g1.send({"type": "ihaveobject", "objectid": UNKNOWN_ID_FOR_IHAVEOBJECT})
        self.assertEqual(await g1.recv(), {"type": "getobject", "objectid": UNKNOWN_ID_FOR_IHAVEOBJECT})

    async def test_invalid_transactions_after_valid_ones(self):
        setup = await self.ready_client()
        await setup.send_raw(COINBASE)
        await setup.send_raw(VALID_TX)
        await setup.send({"type": "getpeers"})
        self.assertEqual((await setup.recv())["type"], "peers")

        cases = [
            ("unknown referenced transaction", UNKNOWN_OUTPOINT, "UNKNOWN_OBJECT", False),
            ("output without value", MISSING_VALUE, "INVALID_FORMAT", True),
            ("output index does not exist", BAD_OUTPUT_INDEX, "INVALID_TX_OUTPOINT", True),
            ("same outpoint twice, outputs too large", SAME_OUTPOINT_TWICE, "INVALID_TX_CONSERVATION", True),
            ("wrong signature", WRONG_SIGNATURE, "INVALID_TX_SIGNATURE", True),
            ("negative output value", NEGATIVE_VALUE, "INVALID_FORMAT", True),
        ]
        for name, line, expected, fatal in cases:
            with self.subTest(name):
                g1 = await self.ready_client()
                g2 = await self.ready_client()
                await g1.send_raw(line)
                msg = await g1.recv()
                self.assertEqual((msg["type"], msg["name"]), ("error", expected), msg)
                if fatal:
                    self.assertTrue(await g1.expect_closed())
                else:  # UNKNOWN_OBJECT: the peer is not faulty
                    await g1.send({"type": "getpeers"})
                    self.assertEqual((await g1.recv())["type"], "peers")
                await self.silent(g2)  # nothing was gossiped
                self.assertFalse(self.node.objects.has(object_id(json.loads(line)["object"])))

    async def test_getobject_for_unknown_transaction_does_not_crash(self):
        g1 = await self.ready_client()
        await g1.send({"type": "getobject", "objectid": UNKNOWN_ID_FOR_GETOBJECT})
        msg = await g1.recv()
        self.assertEqual((msg["type"], msg["name"]), ("error", "UNKNOWN_OBJECT"))
        await g1.send({"type": "getpeers"})
        self.assertEqual((await g1.recv())["type"], "peers")
