"""Write-ahead file receipts with verified staging and restartable publication."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path

from jellyorganize.filesystem.safety import UnsafePath, assert_state, parent_fd, relative_file, root_fd
from jellyorganize.planning.store import FileState, RetainedSource


_active = ContextVar("jellyorganize_move", default=None)


def checkpoint(stage: str) -> None:
    """Fault-injection seam. Production always continues."""


def rename_noreplace(parent: int, source: str, destination: str) -> None:
    # Linux renameat2 publishes a completed staging file without overwriting.
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "atomic no-overwrite rename is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(source), parent, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def new_receipt(file: FileState, source_root: Path, destination_root: Path, mode: str = "move", *, link_only=False,
                retained_state=None) -> dict:
    receipt = {"source_state": file.model_dump(mode="json"), "source_root": str(source_root),
            "destination_root": str(destination_root), "stage": f".jellyorganize-{secrets.token_hex(16)}.part",
            "phase": "intent", "transfer_mode": mode, "link_only": link_only}
    if retained_state is not None:
        receipt["retained_state"] = retained_state.model_dump(mode="json")
    return receipt


def validate_mode(transaction, plan, record=None):
    mode = transaction.data.get("transfer_mode", "move")
    if mode != plan.transfer_mode or (transaction.data.get("version", 1) < 3 and mode != "move"):
        raise UnsafePath("transaction mode does not match saved plan")
    if record is not None and record.get("transfer_mode", "move") != mode:
        raise UnsafePath("item mode does not match saved plan")
    return mode


@contextmanager
def journal_move(transaction, receipt):
    token = _active.set((transaction, receipt))
    try:
        yield
    finally:
        _active.reset(token)


def active_move():
    return _active.get()


def _state(file, current, *, link=None):
    return FileState(source=file.destination, destination=file.source, size=current.st_size,
                     mtime_ns=current.st_mtime_ns, inode=current.st_ino, device=current.st_dev, link=link)


def _exists(descriptor, name):
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def execute(file, source_root, destination_root, config, transaction, receipt, hash_fd):
    if (receipt["source_state"] != file.model_dump(mode="json") or
            receipt["source_root"] != str(source_root) or receipt["destination_root"] != str(destination_root)):
        raise UnsafePath("move receipt does not match file decisions")
    mode = receipt.get("transfer_mode", "move")
    if mode not in {"move", "hardlink", "symlink", "unlink", "unlink-symlink"}:
        raise UnsafePath("unsupported receipt mode")
    stage_name = receipt["stage"]
    if (not stage_name.startswith(".jellyorganize-") or not stage_name.endswith(".part") or
            Path(stage_name).name != stage_name):
        raise UnsafePath("invalid staging filename")
    if mode in {"symlink", "unlink-symlink"} or file.link is not None:
        if mode not in {"move", "symlink", "unlink-symlink"}:
            raise UnsafePath("symbolic links require move or symlink mode")
        return _execute_symlink(file, source_root, destination_root, transaction, receipt)
    transaction.write()
    checkpoint("intent_written")
    with ExitStack() as stack:
        source_descriptor = root_fd(source_root)
        stack.callback(os.close, source_descriptor)
        destination_descriptor = root_fd(destination_root)
        stack.callback(os.close, destination_descriptor)
        source_parent, source_name = stack.enter_context(parent_fd(source_descriptor, relative_file(file.source, source_root)))
        destination_parent, destination_name = stack.enter_context(parent_fd(
            destination_descriptor, relative_file(file.destination, destination_root), create=True))
        destination_state = receipt.get("destination_state")
        if destination_state:
            recorded = FileState.model_validate(destination_state)
            if recorded.source != file.destination or recorded.destination != file.source or recorded.size != file.size:
                raise UnsafePath("destination receipt paths or size do not match the planned move")
        final = _exists(destination_parent, destination_name)
        source = _exists(source_parent, source_name)
        if mode == "unlink":
            # The destination is the retained original. Its recorded identity is
            # required even when a previous attempt already removed the link.
            retained = _state(file, assert_state(destination_parent, destination_name, file))
            if source is not None:
                current = assert_state(source_parent, source_name, file)
                if current.st_nlink < 2 or file.source == file.destination:
                    raise UnsafePath("cannot remove the last retained hard link")
                os.unlink(source_name, dir_fd=source_parent)
                os.fsync(source_parent)
                checkpoint("link_removed")
            receipt["destination_state"] = retained.model_dump(mode="json")
            receipt["phase"] = "moved"
            transaction.write()
            checkpoint("move_recorded")
            return retained
        if final is not None:
            if destination_state is None:
                raise FileExistsError(f"destination exists without our receipt: {file.destination}")
            expected = FileState.model_validate(destination_state)
            assert_state(destination_parent, destination_name, expected)
            if mode == "hardlink":
                assert_state(source_parent, source_name, file)
                if (expected.device, expected.inode) != (file.device, file.inode):
                    raise UnsafePath("destination is not a hard link to the retained source")
            if source is not None and mode == "move":
                assert_state(source_parent, source_name, file)
                os.unlink(source_name, dir_fd=source_parent)
                os.fsync(source_parent)
                checkpoint("source_removed")
            receipt["phase"] = "moved"
            transaction.write()
            checkpoint("move_recorded")
            return expected
        if source is None:
            raise UnsafePath(f"both source and recorded destination are missing: {file.source}")
        if receipt.get("reused"):
            # A link that vanished after preflight must be created by this
            # operation, so its later undo owns that newly published name.
            receipt["reused"] = False
            transaction.write()
        assert_state(source_parent, source_name, file)
        if (mode == "hardlink" or receipt.get("link_only")) and os.fstat(destination_parent).st_dev != file.device:
            raise OSError(errno.EXDEV, "hard links require the same filesystem")
        staged = _exists(destination_parent, stage_name)
        if receipt["phase"] in ("verified", "published", "moved"):
            if staged is None or destination_state is None:
                raise UnsafePath("verified staging file is missing")
            assert_state(destination_parent, stage_name, FileState.model_validate(destination_state))
        else:
            if staged is not None:
                identity = receipt.get("stage_identity")
                ours = (stat.S_ISREG(staged.st_mode) and
                        ((identity and [staged.st_dev, staged.st_ino] == identity) or
                         (staged.st_dev, staged.st_ino) == (file.device, file.inode)))
                if ours:
                    os.unlink(stage_name, dir_fd=destination_parent)
                    os.fsync(destination_parent)
                else:
                    # A crash before recording ownership cannot authorize deletion.
                    # Retain those bytes, allocate another unique stage, and proceed.
                    receipt.setdefault("preserved_stages", []).append(stage_name)
                    stage_name = receipt["stage"] = f".jellyorganize-{secrets.token_hex(16)}.part"
                    transaction.write()
            receipt.pop("stage_identity", None)
            receipt.pop("destination_state", None)
            try:
                os.link(source_name, stage_name, src_dir_fd=source_parent,
                        dst_dir_fd=destination_parent, follow_symlinks=False)
                checkpoint("stage_created")
                staged = os.stat(stage_name, dir_fd=destination_parent, follow_symlinks=False)
                receipt["stage_identity"] = [staged.st_dev, staged.st_ino]
                receipt["phase"] = "staged"
                os.fsync(destination_parent)
                transaction.write()
                checkpoint("stage_recorded")
                # Synchronizing a directory alone does not persist media bytes.
                staged_file = os.open(stage_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=destination_parent)
                stack.callback(os.close, staged_file)
                os.fsync(staged_file)
            except OSError as error:
                if mode == "hardlink" or receipt.get("link_only"):
                    raise
                if error.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.EPERM):
                    raise
                source_file = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent)
                stack.callback(os.close, source_file)
                assert_state(source_parent, source_name, file)
                destination_file = os.open(stage_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                           0o600, dir_fd=destination_parent)
                stack.callback(os.close, destination_file)
                checkpoint("stage_created")
                staged = os.fstat(destination_file)
                receipt["stage_identity"] = [staged.st_dev, staged.st_ino]
                receipt["phase"] = "staged"
                os.fsync(destination_parent)
                transaction.write()
                checkpoint("stage_recorded")
                while block := os.read(source_file, 1024 * 1024):
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_file, view)
                        if written <= 0:
                            raise OSError("copy made no progress")
                        view = view[written:]
                    checkpoint("copy_chunk")
                os.fsync(destination_file)
                if os.fstat(destination_file).st_size != file.size:
                    raise OSError("copied size mismatch")
                source_digest = hash_fd(source_file, config.filesystem.hash_algorithm)
                if source_digest != hash_fd(destination_file, config.filesystem.hash_algorithm):
                    raise OSError("copied hash mismatch")
                receipt["checksum"] = source_digest
                os.fchmod(destination_file, stat.S_IMODE(source.st_mode))
                os.utime(destination_file, ns=(file.mtime_ns, file.mtime_ns))
                os.fsync(destination_file)
            assert_state(source_parent, source_name, file)
            staged = os.stat(stage_name, dir_fd=destination_parent, follow_symlinks=False)
            if not stat.S_ISREG(staged.st_mode) or staged.st_size != file.size:
                raise UnsafePath("staging file changed")
            receipt["destination_state"] = _state(file, staged).model_dump(mode="json")
            receipt["phase"] = "verified"
            transaction.write()
            checkpoint("stage_verified")
        # All completed bytes and their ownership are durable before publication.
        assert_state(source_parent, source_name, file)
        expected = FileState.model_validate(receipt["destination_state"])
        if (mode == "hardlink" or receipt.get("link_only")) and (expected.device, expected.inode) != (file.device, file.inode):
            raise UnsafePath("staged file is not a hard link to the source")
        assert_state(destination_parent, stage_name, expected)
        rename_noreplace(destination_parent, stage_name, destination_name)
        os.fsync(destination_parent)
        checkpoint("published")
        receipt["phase"] = "published"
        transaction.write()
        assert_state(source_parent, source_name, file)
        assert_state(destination_parent, destination_name, expected)
        if mode == "move":
            os.unlink(source_name, dir_fd=source_parent)
            os.fsync(source_parent)
            checkpoint("source_removed")
        else:
            checkpoint("source_retained")
        receipt["phase"] = "moved"
        transaction.write()
        checkpoint("move_recorded")
        return expected


def _execute_symlink(file, source_root, destination_root, transaction, receipt):
    """Publish absolute symlinks without following or copying media bytes."""
    mode = receipt["transfer_mode"]
    link = file.link or RetainedSource(root=source_root, **file.model_dump(exclude={"destination", "link"}))
    transaction.write()
    checkpoint("intent_written")
    with ExitStack() as stack:
        src = root_fd(source_root)
        stack.callback(os.close, src)
        dst = root_fd(destination_root)
        stack.callback(os.close, dst)
        source_parent, source_name = stack.enter_context(parent_fd(src, relative_file(file.source, source_root)))
        destination_parent, destination_name = stack.enter_context(parent_fd(
            dst, relative_file(file.destination, destination_root), create=True))
        if mode == "unlink-symlink":
            retained = FileState.model_validate(receipt.get("retained_state", {}))
            if retained.source != file.destination or retained.destination != file.source or file.link is None:
                raise UnsafePath("invalid retained source for symlink removal")
            if retained.link is not None:
                if retained.link != file.link:
                    raise UnsafePath("retained symlink target changed")
            elif (retained.source != link.source or destination_root != link.root or
                  (retained.size, retained.mtime_ns, retained.inode, retained.device) !=
                  (link.size, link.mtime_ns, link.inode, link.device)):
                raise UnsafePath("retained original does not match symlink target")
            current = assert_state(destination_parent, destination_name, retained)
            if _exists(source_parent, source_name) is not None:
                assert_state(source_parent, source_name, file)
                os.unlink(source_name, dir_fd=source_parent)
                os.fsync(source_parent)
                checkpoint("link_removed")
            restored = _state(file, current, link=retained.link)
            receipt.update(destination_state=restored.model_dump(mode="json"), phase="moved")
            transaction.write()
            checkpoint("move_recorded")
            return restored

        final = _exists(destination_parent, destination_name)
        recorded = receipt.get("destination_state")
        expected = FileState.model_validate(recorded) if recorded else None
        if expected is not None and (expected.source != file.destination or expected.destination != file.source or expected.link != link):
            raise UnsafePath("symbolic link receipt does not match source")
        if final is not None:
            if expected is None:
                raise FileExistsError(f"destination exists without our receipt: {file.destination}")
            assert_state(destination_parent, destination_name, expected)
            if mode == "symlink" or _exists(source_parent, source_name) is not None:
                assert_state(source_parent, source_name, file)
                if mode == "move":
                    os.unlink(source_name, dir_fd=source_parent)
                    os.fsync(source_parent)
                    checkpoint("source_removed")
            receipt["phase"] = "moved"
            transaction.write()
            checkpoint("move_recorded")
            return expected
        assert_state(source_parent, source_name, file)
        if receipt.get("reused"):
            receipt["reused"] = False
            transaction.write()
        stage = receipt["stage"]
        staged = _exists(destination_parent, stage)
        if receipt["phase"] in {"verified", "published", "moved"}:
            if staged is None or expected is None:
                raise UnsafePath("verified symbolic link stage is missing")
            assert_state(destination_parent, stage, expected)
        else:
            if staged is not None:
                if stat.S_ISLNK(staged.st_mode) and receipt.get("stage_identity") == [staged.st_dev, staged.st_ino]:
                    os.unlink(stage, dir_fd=destination_parent)
                    os.fsync(destination_parent)
                else:
                    receipt.setdefault("preserved_stages", []).append(stage)
                    stage = receipt["stage"] = f".jellyorganize-{secrets.token_hex(16)}.part"
                    transaction.write()
            receipt.pop("destination_state", None)
            receipt.pop("stage_identity", None)
            os.symlink(str(link.source), stage, dir_fd=destination_parent)
            checkpoint("stage_created")
            staged = os.stat(stage, dir_fd=destination_parent, follow_symlinks=False)
            receipt.update(stage_identity=[staged.st_dev, staged.st_ino], phase="staged")
            os.fsync(destination_parent)
            transaction.write()
            checkpoint("stage_recorded")
            assert_state(source_parent, source_name, file)
            expected = _state(file, staged, link=link)
            assert_state(destination_parent, stage, expected)
            receipt.update(destination_state=expected.model_dump(mode="json"), phase="verified")
            transaction.write()
            checkpoint("stage_verified")
        assert_state(source_parent, source_name, file)
        assert_state(destination_parent, stage, expected)
        rename_noreplace(destination_parent, stage, destination_name)
        os.fsync(destination_parent)
        checkpoint("published")
        receipt["phase"] = "published"
        transaction.write()
        assert_state(source_parent, source_name, file)
        assert_state(destination_parent, destination_name, expected)
        if mode == "move":
            os.unlink(source_name, dir_fd=source_parent)
            os.fsync(source_parent)
            checkpoint("source_removed")
        else:
            checkpoint("source_retained")
        receipt["phase"] = "moved"
        transaction.write()
        checkpoint("move_recorded")
        return expected


def discard_stage(file, destination_root, transaction, receipt):
    """Remove only a stage whose ownership was recorded, retaining uncertain data."""
    stage = receipt.get("stage")
    if not stage or Path(stage).name != stage:
        return
    with ExitStack() as stack:
        descriptor = root_fd(destination_root)
        stack.callback(os.close, descriptor)
        parent, _ = stack.enter_context(parent_fd(descriptor, relative_file(file.destination, destination_root)))
        current = _exists(parent, stage)
        identity = receipt.get("stage_identity")
        if current is not None and identity == [current.st_dev, current.st_ino] and (
                stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode)):
            os.unlink(stage, dir_fd=parent)
            os.fsync(parent)
            receipt["phase"] = "discarded"
            transaction.write()
