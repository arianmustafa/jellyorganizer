"""Keep scheduled runs and undo from executing moves concurrently."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def media_lock(transaction_root: Path):
    transaction_root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(transaction_root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another apply or undo is running; try again after it completes") from error
        yield
    finally:
        os.close(descriptor)
