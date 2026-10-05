"""Persist file observations and downloader completion acknowledgements."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

from jellyorganize.planning.store import snapshot


class CompletionTracker:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS observations (
                source TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                unchanged_since REAL NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)""")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def fingerprint(item):
        states = [snapshot(path).model_dump(mode="json") for path in sorted([item.path, *item.sidecars])]
        return hashlib.sha256(json.dumps(states, sort_keys=True).encode()).hexdigest()

    def acknowledge(self, item):
        fingerprint = self.fingerprint(item)
        with self.database() as connection:
            connection.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, 1)",
                               (str(item.path), fingerprint, time.time()))

    def eligible(self, item, incoming, *, now=None):
        if incoming.completion == "handoff" or (incoming.completion == "stable" and incoming.stability_seconds == 0):
            return True
        now = time.time() if now is None else now
        fingerprint = self.fingerprint(item)
        with self.database() as connection:
            row = connection.execute("SELECT fingerprint, unchanged_since, acknowledged FROM observations WHERE source=?",
                                     (str(item.path),)).fetchone()
            if row is None or row[0] != fingerprint:
                connection.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, 0)",
                                   (str(item.path), fingerprint, now))
                return False
            if row[2]:
                return True
            return incoming.completion == "stable" and now - row[1] >= incoming.stability_seconds

    def filter(self, scan, incoming):
        for item in scan.items:
            if item.reason:
                continue
            try:
                if not self.eligible(item, incoming):
                    item.reason = ("waiting for downloader completion acknowledgement" if incoming.completion == "marker"
                                   else "waiting for files and sidecars to remain stable across runs")
            except (OSError, ValueError) as error:
                item.reason = f"completion check failed: {error}"
        return scan

    def wait_seconds(self, item, incoming):
        """Delay for one bounded observation window in a manual invocation."""
        now = time.time()
        eligible = self.eligible(item, incoming, now=now)
        stability = 0
        if not eligible:
            if incoming.completion != "stable":
                return 0
            with self.database() as connection:
                since = connection.execute("SELECT unchanged_since FROM observations WHERE source=?",
                                           (str(item.path),)).fetchone()[0]
            stability = min(incoming.stability_seconds, max(0, incoming.stability_seconds - (now - since)))
        newest = max(snapshot(path).mtime_ns for path in [item.path, *item.sidecars]) / 1e9
        age = min(incoming.minimum_age_seconds, max(0, incoming.minimum_age_seconds - (now - newest)))
        return max(stability, age)
