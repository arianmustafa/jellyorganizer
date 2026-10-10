"""Persistent run history and deduplicated exceptions for unattended use."""

import json
import sqlite3
import time
from contextlib import contextmanager


class Operations:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, finished_at REAL NOT NULL,
                exit_code INTEGER NOT NULL, plan_id TEXT, summary TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS exceptions (
                source TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                reason TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                occurrences INTEGER NOT NULL, plan_id TEXT, resolved_at REAL)""")
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, plan, exit_code, apply_counts=None, transaction=None, *, recovered_items=0):
        now = time.time()
        issues = {str(entry.source): (entry.kind, entry.status, entry.reason) for entry in plan.entries
                  if entry.status in {"REVIEW", "CONFLICT", "ERROR"}}
        if transaction:
            for item in transaction.data["items"]:
                if item["status"] not in {"applied", "undone"}:
                    issues[item["source"]] = (item.get("kind", "movie"), item["status"].upper(), item.get("error", "apply failed"))
        counts = {key: sum(entry.status == key for entry in plan.entries)
                  for key in ("CONFIRMED", "REVIEW", "CONFLICT", "ERROR", "SKIP")}
        summary = {"plan_id": plan.plan_id, "transfer_mode": plan.transfer_mode, "exit_code": exit_code, "counts": counts,
                   "apply": apply_counts or {}, "changed_exceptions": 0, "scope": plan.scope,
                   "recovered_items": recovered_items,
                   "transaction_id": transaction.data["transaction_id"] if transaction else None}
        with self.database() as connection:
            # Resolve only sources actually covered by this run. Missing or
            # unreadable roots must not erase previous exceptions.
            covered = {str(entry.source) for entry in plan.entries}
            for row in connection.execute("SELECT source FROM exceptions WHERE resolved_at IS NULL").fetchall():
                if row["source"] in covered and row["source"] not in issues:
                    connection.execute("UPDATE exceptions SET resolved_at=? WHERE source=?", (now, row["source"]))
            for source, (kind, status, reason) in issues.items():
                row = connection.execute("SELECT * FROM exceptions WHERE source=?", (source,)).fetchone()
                if row is None or row["resolved_at"] is not None or (row["status"], row["reason"]) != (status, reason):
                    summary["changed_exceptions"] += 1
                connection.execute("""INSERT INTO exceptions VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL)
                    ON CONFLICT(source) DO UPDATE SET kind=excluded.kind, status=excluded.status,
                    reason=excluded.reason, last_seen=excluded.last_seen,
                    occurrences=exceptions.occurrences+1, plan_id=excluded.plan_id, resolved_at=NULL""",
                    (source, kind, status, reason, now, now, plan.plan_id))
            connection.execute("INSERT INTO runs(finished_at, exit_code, plan_id, summary) VALUES (?, ?, ?, ?)",
                               (now, exit_code, plan.plan_id, json.dumps(summary)))
            # Run summaries are disposable; plans/journals remain for undo.
            connection.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 1000)")
        return summary

    def failure(self, reason):
        now = time.time()
        summary = {"exit_code": 1, "error": reason}
        with self.database() as connection:
            connection.execute("INSERT INTO runs(finished_at, exit_code, summary) VALUES (?, 1, ?)",
                               (now, json.dumps(summary)))
            connection.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 1000)")

    def exceptions(self):
        with self.database() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM exceptions WHERE resolved_at IS NULL ORDER BY first_seen")]

    def status(self):
        with self.database() as connection:
            last = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            healthy = connection.execute("SELECT finished_at FROM runs WHERE exit_code IN (0,2,3) ORDER BY id DESC LIMIT 1").fetchone()
            return {"last_run": ({**dict(last), "summary": json.loads(last["summary"])} if last else None),
                    "last_healthy_run": healthy[0] if healthy else None,
                    "open_exceptions": connection.execute("SELECT COUNT(*) FROM exceptions WHERE resolved_at IS NULL").fetchone()[0]}
