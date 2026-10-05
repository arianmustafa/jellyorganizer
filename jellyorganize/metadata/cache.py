"""SQLite cache for normalized provider responses. No credentials are stored."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class MetadataCache:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute("""CREATE TABLE IF NOT EXISTS entries (
            provider TEXT NOT NULL, query TEXT NOT NULL, entity_id TEXT,
            response TEXT NOT NULL, retrieved REAL NOT NULL, expires REAL NOT NULL,
            PRIMARY KEY (provider, query))""")
        return connection

    def get(self, provider: str, query: dict) -> object | None:
        if not self.path.exists():
            return None
        key = json.dumps(query, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            row = connection.execute("SELECT response, expires FROM entries WHERE provider=? AND query=?", (provider, key)).fetchone()
        return json.loads(row[0]) if row and row[1] > time.time() else None

    def put(self, provider: str, query: dict, response: object, *, entity_id: str | None = None, days: int = 30) -> None:
        key = json.dumps(query, sort_keys=True, separators=(",", ":"))
        now = time.time()
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                               (provider, key, entity_id, json.dumps(response), now, now + days * 86400))

    def stats(self) -> tuple[int, int]:
        if not self.path.exists():
            return 0, 0
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            valid = connection.execute("SELECT COUNT(*) FROM entries WHERE expires>?", (time.time(),)).fetchone()[0]
        return total, valid

    def clear(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            connection.execute("DELETE FROM entries")
        return count
