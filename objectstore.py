"""Persistent object database (SQLite), keyed by objectid."""

import json
import logging
import os
import sqlite3

from objects import GENESIS, GENESIS_ID
from protocol import canonical_json

log = logging.getLogger("kerma.objects")


class ObjectStore:
    def __init__(self, path):
        if path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        # One row per stored block: its height and the UTXO set after executing it.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS block_state "
            "(id TEXT PRIMARY KEY, height INTEGER NOT NULL, utxo TEXT NOT NULL)"
        )
        self._db.commit()
        self.put_block(GENESIS_ID, GENESIS, 0, set())  # every node knows the genesis block
        log.info("object database %s holds %d objects", path, len(self))

    def __len__(self):
        return self._db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]

    def has(self, objectid):
        return self._db.execute("SELECT 1 FROM objects WHERE id = ?", (objectid,)).fetchone() is not None

    def get(self, objectid):
        """The stored object (as a dict), or None if unknown."""
        row = self._db.execute("SELECT data FROM objects WHERE id = ?", (objectid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, objectid, obj):
        """Store an object. Returns True if it was new."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO objects (id, data) VALUES (?, ?)",
            (objectid, canonical_json(obj).decode("utf-8")),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def put_block(self, blockid, block, height, utxo):
        """Store a validated block together with its height and the UTXO set after it.

        `utxo` is a set of (txid, index) pairs. Returns True if the block was new.
        """
        utxo_json = json.dumps(sorted(utxo), separators=(",", ":"))
        with self._db:  # one transaction: the object and its state are stored together or not at all
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO objects (id, data) VALUES (?, ?)",
                (blockid, canonical_json(block).decode("utf-8")),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO block_state (id, height, utxo) VALUES (?, ?, ?)",
                (blockid, height, utxo_json),
            )
        return cursor.rowcount == 1

    def block_state(self, blockid):
        """(height, utxo set) of a stored block, or None if there is no such block."""
        row = self._db.execute("SELECT height, utxo FROM block_state WHERE id = ?", (blockid,)).fetchone()
        if row is None:
            return None
        return row[0], {(txid, index) for txid, index in json.loads(row[1])}

    def ids(self):
        return [row[0] for row in self._db.execute("SELECT id FROM objects")]

    def close(self):
        self._db.close()
