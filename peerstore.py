"""Persistent set of known peers.

Stored as JSON on disk (written atomically) so the list survives restarts.
Each peer keeps a few bookkeeping fields used by the sharing / connection
policies:

* last_seen    - when we last heard about the peer (peers message, bootstrap)
* last_success - when we last completed a handshake with it (we dialed it)
* failures     - consecutive failed connection attempts
* last_attempt - when we last tried to dial it
"""

import json
import logging
import os
import time

from protocol import split_peer_address

log = logging.getLogger("kerma.peers")


class PeerStore:
    def __init__(self, path, max_known=1000, max_failures=3):
        self.path = path
        self.max_known = max_known
        self.max_failures = max_failures
        self._peers = {}
        self._load()

    # -- container helpers -------------------------------------------------

    def __len__(self):
        return len(self._peers)

    def __contains__(self, addr):
        return addr in self._peers

    def addresses(self):
        return list(self._peers)

    # -- persistence -------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("could not read %s, starting with an empty peer list", self.path)
            return
        peers = data.get("peers") if isinstance(data, dict) else None
        if not isinstance(peers, dict):
            return
        for addr, info in peers.items():
            try:
                split_peer_address(addr)
            except ValueError:
                continue
            info = info if isinstance(info, dict) else {}
            self._peers[addr] = {
                "last_seen": _num(info, "last_seen"),
                "last_success": _num(info, "last_success"),
                "failures": int(_num(info, "failures")),
                "last_attempt": _num(info, "last_attempt"),
            }
        log.info("loaded %d known peers", len(self._peers))

    def save(self):
        tmp = self.path + ".tmp"
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"peers": self._peers}, f)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not save peers to %s", self.path)

    # -- mutation ----------------------------------------------------------

    def add(self, addr, *, trusted=False, now=None, save=True):
        """Remember a peer. Returns True if it was new.

        Untrusted addresses (from the network) must be syntactically valid.
        `trusted` is for addresses we configured ourselves (bootstrap, tests).
        """
        if not trusted:
            try:
                split_peer_address(addr)
            except ValueError:
                return False
        now = time.time() if now is None else now
        entry = self._peers.get(addr)
        if entry is not None:
            entry["last_seen"] = now
            if save:
                self.save()
            return False
        if len(self._peers) >= self.max_known:
            self._evict_one()
        self._peers[addr] = {
            "last_seen": now,
            "last_success": 0.0,
            "failures": 0,
            "last_attempt": 0.0,
        }
        if save:
            self.save()
        return True

    def add_many(self, addrs):
        for addr in addrs:
            self.add(addr, save=False)
        self.save()

    def _evict_one(self):
        worst = min(
            self._peers,
            key=lambda a: (self._peers[a]["last_success"], self._peers[a]["last_seen"]),
        )
        del self._peers[worst]

    def mark_success(self, addr):
        entry = self._peers.get(addr)
        if entry is not None:
            entry["last_success"] = time.time()
            entry["failures"] = 0
            self.save()

    def record_attempt(self, addr):
        entry = self._peers.get(addr)
        if entry is not None:
            entry["last_attempt"] = time.time()
            self.save()

    def record_failure(self, addr):
        """Count a failed connection attempt. Returns True if the peer was forgotten."""
        entry = self._peers.get(addr)
        if entry is None:
            return False
        entry["failures"] += 1
        if entry["failures"] >= self.max_failures:
            del self._peers[addr]
            self.save()
            return True
        self.save()
        return False

    def forget(self, addr):
        if self._peers.pop(addr, None) is not None:
            self.save()

    # -- policies ----------------------------------------------------------

    def select_for_sharing(self, limit):
        """Peers to put in a `peers` message: ones we handshook with first, then newest."""
        ranked = sorted(
            self._peers.items(),
            key=lambda kv: (kv[1]["last_success"], kv[1]["last_seen"]),
            reverse=True,
        )
        return [addr for addr, _ in ranked[:limit]]

    def candidates(self, exclude, now, retry_after):
        """Peers we may dial now: not excluded and not tried within `retry_after` seconds."""
        return [
            addr
            for addr, entry in self._peers.items()
            if addr not in exclude and now - entry["last_attempt"] >= retry_after
        ]


def _num(info, key):
    value = info.get(key, 0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
