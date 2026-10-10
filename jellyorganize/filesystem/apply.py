"""Apply saved decisions with no-overwrite moves and per-item rollback."""

from __future__ import annotations

import errno
import hashlib
import os
from contextlib import ExitStack
from pathlib import Path

from jellyorganize.config import Config
from jellyorganize.filesystem.locking import media_lock
from jellyorganize.filesystem.safety import UnsafePath, assert_state, destination_free, parent_fd, relative_file, root_fd
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.filesystem.durable import active_move, execute, journal_move, new_receipt, discard_stage, checkpoint
from jellyorganize.planning.store import FileState, PlanEntry, SavedPlan, configured_roots
from jellyorganize.parsing.episode_codes import coverage, overlaps
from jellyorganize.scanner.sidecars import MEDIA_EXTENSIONS


def _release_conflict(entry: PlanEntry, ignored=()) -> bool:
    if entry.kind == "download":
        return False  # Handoffs check every exact destination; identities are resolved in Incoming.
    if entry.destination is None:
        return True
    if entry.kind == "tv":
        source_code, target_code = coverage(entry.source), coverage(entry.destination)
        if source_code and target_code and source_code != target_code and any(
            path != entry.source and path.suffix.lower() in MEDIA_EXTENSIONS and
            (path.is_file() or path.is_symlink()) and overlaps(entry.source, path)
            for path in entry.source.parent.iterdir()
        ):
            return True
    parent = entry.destination.parent
    if not parent.exists():
        return False
    if parent.is_symlink() or not parent.is_dir():
        return True
    if entry.kind == "movie":
        from jellyorganize.naming.movie_versions import movie_conflict
        return movie_conflict(entry.destination, entry.source, ignored)
    prefix = " - ".join(entry.destination.stem.split(" - ")[:2])
    target_coverage = coverage(entry.destination)
    return any(path != entry.source and path not in ignored and path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
               and (path.stem == prefix or path.stem.startswith(prefix + " - ") or
                    overlaps(entry.destination, path) or
                    (target_coverage and len(target_coverage[1]) > 1 and coverage(path) is None))
               for path in parent.iterdir())


def _validate_entry(entry: PlanEntry, config: Config, workflow: str, *, mode="move", links=None) -> None:
    expected_source, expected_destination = configured_roots(config, entry.kind, workflow)
    if entry.source_root != expected_source or entry.destination_root != expected_destination:
        raise UnsafePath("plan roots no longer match configuration")
    if entry.destination is None or not entry.files or entry.files[0].source != entry.source or entry.files[0].destination != entry.destination:
        raise UnsafePath("plan has incomplete file decisions")
    sources = [file.source for file in entry.files]
    destinations = [file.destination for file in entry.files]
    if len(sources) != len(set(sources)) or len(destinations) != len(set(destinations)):
        raise UnsafePath("duplicate file path in plan")
    with ExitStack() as stack:
        source_root = root_fd(entry.source_root)
        stack.callback(os.close, source_root)
        destination_root = root_fd(entry.destination_root)
        stack.callback(os.close, destination_root)
        for file in entry.files:
            source_relative = relative_file(file.source, entry.source_root)
            relative_file(file.destination, entry.destination_root)
            with parent_fd(source_root, source_relative) as (descriptor, name):
                assert_state(descriptor, name, file)
            if file.source != file.destination and (file.destination.exists() or file.destination.is_symlink()):
                if mode != "hardlink" or links is None or links.owned(file, entry.source_root, entry.destination_root) is None:
                    raise FileExistsError(f"destination exists: {file.destination}")
            if mode == "hardlink" or (links is not None and workflow == "audit" and links.protected(file)):
                relative = relative_file(file.destination, entry.destination_root)
                while True:
                    try:
                        with parent_fd(destination_root, relative) as (descriptor, _):
                            if os.fstat(descriptor).st_dev != file.device:
                                raise OSError(errno.EXDEV, "hard links require the same filesystem")
                        break
                    except FileNotFoundError:
                        relative = relative.parent
    ignored = {file.destination for file in entry.files if mode == "hardlink" and links is not None and
               links.owned(file, entry.source_root, entry.destination_root) is not None}
    if _release_conflict(entry, ignored):
        raise FileExistsError(f"possible duplicate release: {entry.destination}")


def _hash_fd(descriptor: int, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, 1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def _check_unchanged(file: FileState, root: Path) -> None:
    with ExitStack() as stack:
        root_descriptor = root_fd(root)
        stack.callback(os.close, root_descriptor)
        descriptor, name = stack.enter_context(parent_fd(root_descriptor, relative_file(file.source, root)))
        assert_state(descriptor, name, file)


def _move_file(file: FileState, source_root: Path, destination_root: Path, config: Config) -> FileState:
    """Move one file. On failure before source removal, remove only our new destination."""
    active = active_move()
    if active is not None:
        transaction, receipt = active
        return execute(file, source_root, destination_root, config, transaction, receipt, _hash_fd)
    source_relative = relative_file(file.source, source_root)
    destination_relative = relative_file(file.destination, destination_root)
    with ExitStack() as stack:
        source_descriptor = root_fd(source_root)
        destination_descriptor = root_fd(destination_root)
        stack.callback(os.close, source_descriptor)
        stack.callback(os.close, destination_descriptor)
        source_parent, source_name = stack.enter_context(parent_fd(source_descriptor, source_relative))
        destination_parent, destination_name = stack.enter_context(parent_fd(destination_descriptor, destination_relative, create=True))
        assert_state(source_parent, source_name, file)
        if not destination_free(destination_parent, destination_name):
            raise FileExistsError(f"destination exists: {file.destination}")
        made_destination = False
        created_identity: tuple[int, int] | None = None
        source_removed = False
        try:
            try:
                os.link(source_name, destination_name, src_dir_fd=source_parent,
                        dst_dir_fd=destination_parent, follow_symlinks=False)
                made_destination = True
                created = os.stat(destination_name, dir_fd=destination_parent, follow_symlinks=False)
                created_identity = created.st_dev, created.st_ino
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                source_file = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent)
                try:
                    assert_state(source_parent, source_name, file)
                    destination_file = os.open(destination_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                               0o644, dir_fd=destination_parent)
                    made_destination = True
                    created = os.fstat(destination_file)
                    created_identity = created.st_dev, created.st_ino
                    try:
                        os.lseek(source_file, 0, os.SEEK_SET)
                        while block := os.read(source_file, 1024 * 1024):
                            view = memoryview(block)
                            while view:
                                written = os.write(destination_file, view)
                                if written <= 0:
                                    raise OSError("copy made no progress")
                                view = view[written:]
                        os.fsync(destination_file)
                    finally:
                        os.close(destination_file)
                    copied = os.stat(destination_name, dir_fd=destination_parent, follow_symlinks=False)
                    if copied.st_size != file.size:
                        raise OSError("copied size mismatch")
                    if config.filesystem.verify_cross_filesystem_copy:
                        copied_file = os.open(destination_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=destination_parent)
                        try:
                            if _hash_fd(source_file, config.filesystem.hash_algorithm) != _hash_fd(copied_file, config.filesystem.hash_algorithm):
                                raise OSError("copied hash mismatch")
                        finally:
                            os.close(copied_file)
                    os.utime(destination_name, ns=(file.mtime_ns, file.mtime_ns), dir_fd=destination_parent, follow_symlinks=False)
                finally:
                    os.close(source_file)
            current = os.stat(destination_name, dir_fd=destination_parent, follow_symlinks=False)
            if current.st_size != file.size or (current.st_dev, current.st_ino) != created_identity:
                raise OSError("destination size changed")
            assert_state(source_parent, source_name, file)
            os.fsync(destination_parent)
            os.unlink(source_name, dir_fd=source_parent)
            source_removed = True
            return FileState(source=file.destination, destination=file.source, size=current.st_size,
                             mtime_ns=current.st_mtime_ns, inode=current.st_ino, device=current.st_dev)
        except BaseException:
            if made_destination and not source_removed:
                # Remove our new path only while the original source and the
                # destination inode both still match. Otherwise keep both for
                # manual inspection; deleting either could lose user data.
                try:
                    assert_state(source_parent, source_name, file)
                    current = os.stat(destination_name, dir_fd=destination_parent, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == created_identity:
                        os.unlink(destination_name, dir_fd=destination_parent)
                except OSError:
                    pass
                except UnsafePath:
                    pass
            raise


def apply_plan(plan: SavedPlan, config: Config, transaction_root: Path,
               *, auto_threshold: float | None = None) -> tuple[Transaction, dict[str, int]]:
    if plan.workflow == "handoff":
        raise ValueError("handoff plans must use import-download so the current torrent state is checked again")
    with media_lock(transaction_root):
        from jellyorganize.filesystem.recovery import recover_pending
        recover_pending(config, transaction_root, fail_on_blocked=True)
        return _apply_plan(plan, config, transaction_root, auto_threshold=auto_threshold)


def _apply_plan(plan: SavedPlan, config: Config, transaction_root: Path,
                *, auto_threshold: float | None = None) -> tuple[Transaction, dict[str, int]]:
    plan = SavedPlan.model_validate(plan.model_dump(mode="json"))
    transaction = Transaction(transaction_root, plan.plan_id)
    transaction.data["transfer_mode"] = plan.transfer_mode
    from jellyorganize.integrations import refresh_settings
    settings = refresh_settings(config)
    if settings is not None and plan.workflow in {"ingest", "audit"}:
        transaction.data["jellyfin_refresh"] = settings
        transaction.write()
    from jellyorganize.filesystem.links import LinkedImports
    links = LinkedImports(config)
    if auto_threshold is not None:
        transaction.data["auto_threshold"] = auto_threshold
        transaction.write()
    counts = {"APPLIED": 0, "STALE": 0, "CONFLICT": 0, "ERROR": 0, "UNTOUCHED": 0}
    for entry in plan.entries:
        if entry.status != "CONFIRMED" or (auto_threshold is not None and
           (entry.candidate is None or entry.candidate.provider != "tmdb" or entry.confidence < auto_threshold)):
            counts["UNTOUCHED"] += 1
            continue
        record = {"operation": plan.workflow, "source": str(entry.source), "destination": str(entry.destination), "kind": entry.kind,
                  "candidate": entry.candidate.model_dump(mode="json") if entry.candidate else None,
                  "files": [], "status": "started", "moves": [], "transfer_mode": plan.transfer_mode}
        transaction.append(record)
        moved: list[FileState] = []
        try:
            _validate_entry(entry, config, plan.workflow, mode=plan.transfer_mode, links=links)
            ordered = [*entry.files[1:], entry.files[0]]
            record["moves"] = [new_receipt(file, entry.source_root, entry.destination_root, plan.transfer_mode,
                                          link_only=plan.workflow == "audit" and links.protected(file))
                               for file in ordered if file.source != file.destination]
            if plan.transfer_mode == "hardlink":
                for receipt in record["moves"]:
                    file = FileState.model_validate(receipt["source_state"])
                    owned = links.owned(file, entry.source_root, entry.destination_root)
                    if owned is not None:
                        receipt.update(destination_state=owned.model_dump(mode="json"), reused=True)
            transaction.write()
            # Sidecars first: a failure leaves the media file at its source.
            for file in ordered:
                if file.source == file.destination:
                    _check_unchanged(file, entry.source_root)
                    record["files"].append({"source": str(file.source), "destination": str(file.destination),
                                            "source_state": file.model_dump(mode="json"), "status": "unchanged"})
                    transaction.write()
                    continue
                receipt = next(move for move in record["moves"] if move["source_state"]["source"] == str(file.source))
                with journal_move(transaction, receipt):
                    reversed_state = _move_file(file, entry.source_root, entry.destination_root, config)
                moved.append(reversed_state)
                record["files"].append({"source": str(file.source), "destination": str(file.destination),
                                        "source_state": file.model_dump(mode="json"),
                                        "destination_state": reversed_state.model_dump(mode="json"), "status": "moved",
                                        "reused": receipt.get("reused", False)})
                transaction.write()
            record["status"] = "applied"
            transaction.write()
            checkpoint("item_committed")
            counts["APPLIED"] += 1
        except (UnsafePath, FileNotFoundError) as error:
            record["status"] = "stale"
            record["error"] = str(error)
            counts["STALE"] += 1
        except FileExistsError as error:
            record["status"] = "conflict"
            record["error"] = str(error)
            counts["CONFLICT"] += 1
        except (OSError, ValueError) as error:
            record["status"] = "error"
            record["error"] = str(error)
            counts["ERROR"] += 1
        finally:
            if record["status"] != "applied" and moved:
                for reversed_state in reversed(moved):
                    try:
                        receipt = next(move for move in record["moves"] if move["source_state"]["source"] == str(reversed_state.destination))
                        if receipt.get("reused"):
                            continue
                        rollback = receipt["rollback"] = new_receipt(reversed_state, entry.destination_root, entry.source_root,
                                                                     "unlink" if plan.transfer_mode == "hardlink" else "move",
                                                                     link_only=receipt.get("link_only", False))
                        with journal_move(transaction, rollback):
                            _move_file(reversed_state, entry.destination_root, entry.source_root, config)
                        receipt["phase"] = "rolled_back"
                        record["files"].append({"source": str(reversed_state.source), "destination": str(reversed_state.destination), "status": "rolled back"})
                    except (OSError, ValueError) as rollback_error:
                        record["status"] = "error"
                        record["rollback_error"] = str(rollback_error)
                        counts["ERROR"] += 1
            if record["status"] != "applied":
                for receipt in record["moves"]:
                    file = FileState.model_validate(receipt["source_state"])
                    try:
                        _check_unchanged(file, entry.source_root)
                        discard_stage(file, entry.destination_root, transaction, receipt)
                    except (OSError, ValueError):
                        pass
                record["destination_files_present"] = [str(file.destination) for file in entry.files
                                                       if file.destination.exists() or file.destination.is_symlink()]
            transaction.write()
    transaction.complete()
    checkpoint("transaction_committed")
    return transaction, counts
