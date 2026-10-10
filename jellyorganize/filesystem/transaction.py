"""Durable per-apply journal, independent of the immutable plan."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Transaction:
    @classmethod
    def load(cls, root: Path, transaction_id: str) -> "Transaction":
        if not transaction_id or any(character not in "0123456789abcdef-" for character in transaction_id):
            raise ValueError("invalid transaction ID")
        transaction = cls.__new__(cls)
        transaction.path = root / f"{transaction_id}.json"
        transaction.data = json.loads(transaction.path.read_text(encoding="utf-8"))
        if (not isinstance(transaction.data, dict) or transaction.data.get("transaction_id") != transaction_id or
                not isinstance(transaction.data.get("plan_id"), str) or
                not isinstance(transaction.data.get("items"), list) or
                not all(isinstance(item, dict) for item in transaction.data["items"])):
            raise ValueError("invalid transaction journal")
        if transaction.data.get("version", 1) not in (1, 2, 3):
            raise ValueError("unsupported transaction journal version")
        return transaction

    def __init__(self, root: Path, plan_id: str):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4) + ".json")
        self.data = {"version": 3, "transfer_mode": "move", "transaction_id": self.path.stem, "plan_id": plan_id,
                     "created_at": datetime.now(timezone.utc).isoformat(), "items": []}
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self.write()

    def write(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".transaction-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(self.data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        sync_directory(self.path.parent)

    def complete(self) -> None:
        self.data["completed_at"] = datetime.now(timezone.utc).isoformat()
        self.write()

    def append(self, item: dict) -> None:
        self.data["items"].append(item)
        self.write()
