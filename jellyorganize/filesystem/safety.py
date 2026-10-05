"""Root-bound path checks and no-follow directory traversal for apply."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from jellyorganize.planning.store import FileState


class UnsafePath(ValueError):
    pass


def relative_file(path: Path, root: Path) -> Path:
    if not path.is_absolute() or not root.is_absolute():
        raise UnsafePath("plan paths must be absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise UnsafePath(f"path escapes configured root: {path}") from error
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise UnsafePath(f"unsafe relative path: {path}")
    return relative


def root_fd(root: Path) -> int:
    if root.is_symlink():
        raise UnsafePath(f"symlinked root: {root}")
    # A system may symlink the user's home directory (for example /home/user
    # to /home22/user). Resolve that configured ancestor, then anchor all
    # subsequent traversal to a no-follow descriptor for the actual root.
    resolved = root.resolve(strict=True)
    descriptor = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if root.is_symlink() or root.resolve(strict=True) != resolved:
            raise UnsafePath(f"configured root changed: {root}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def parent_fd(root_descriptor: int, relative: Path, *, create: bool = False):
    descriptor = os.dup(root_descriptor)
    try:
        for part in relative.parts[:-1]:
            if part in ("", ".", ".."):
                raise UnsafePath("unsafe path component")
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, relative.name
    finally:
        os.close(descriptor)


def assert_state(descriptor: int, name: str, expected: FileState) -> os.stat_result:
    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        raise UnsafePath(f"not a regular file: {expected.source}")
    if (current.st_size, current.st_mtime_ns, current.st_ino, current.st_dev) != (
        expected.size, expected.mtime_ns, expected.inode, expected.device
    ):
        raise UnsafePath(f"stale source: {expected.source}")
    return current


def destination_free(descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False
