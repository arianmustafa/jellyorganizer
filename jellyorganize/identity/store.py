"""Manually confirmed identities and permanent skips, separate from API cache."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from jellyorganize.metadata.matcher import local_title_year, normalize
from jellyorganize.models import Candidate, MediaItem


class IdentityStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute("""CREATE TABLE IF NOT EXISTS identities (
            media_type TEXT NOT NULL, hint_key TEXT NOT NULL, year_key INTEGER NOT NULL,
            candidate TEXT, skipped INTEGER NOT NULL DEFAULT 0,
            confirmed_at REAL NOT NULL, confirmation_source TEXT NOT NULL,
            PRIMARY KEY (media_type, hint_key, year_key))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS skipped_paths (
            media_type TEXT NOT NULL, source_path TEXT NOT NULL,
            skipped_at REAL NOT NULL,
            PRIMARY KEY (media_type, source_path))""")
        return connection

    @staticmethod
    def key(item: MediaItem) -> tuple[str, str, int]:
        title, year = local_title_year(item)
        hint = normalize(title or item.path.stem)
        if not hint:
            raise ValueError("cannot identify an item without a usable title")
        return item.kind, hint, year or 0

    def lookup(self, item: MediaItem) -> tuple[Candidate | None, bool]:
        if not self.path.exists():
            return None, False
        with self._connect() as connection:
            skipped = connection.execute(
                "SELECT 1 FROM skipped_paths WHERE media_type=? AND source_path=?",
                (item.kind, str(item.path.absolute()))).fetchone()
            row = connection.execute("SELECT candidate, skipped FROM identities WHERE media_type=? AND hint_key=? AND year_key=?",
                                     self.key(item)).fetchone()
        # Legacy title-wide skips are intentionally ignored. They could hide
        # an entire series when the user skipped one featurette.
        return Candidate.model_validate_json(row[0]) if row and row[0] else None, bool(skipped)

    def confirm(self, item: MediaItem, candidate: Candidate, source: str) -> None:
        if candidate.provider != "tmdb" or candidate.kind != item.kind:
            raise ValueError("manual identity must be a matching TMDb entity")
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO identities VALUES (?, ?, ?, ?, 0, ?, ?)",
                               (*self.key(item), candidate.model_dump_json(), time.time(), source))
            connection.execute("DELETE FROM skipped_paths WHERE media_type=? AND source_path=?",
                               (item.kind, str(item.path.absolute())))

    def skip_permanently(self, item: MediaItem) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO skipped_paths VALUES (?, ?, ?)",
                               (item.kind, str(item.path.absolute()), time.time()))
