"""Resume interrupted apply/undo from immutable plans and write-ahead receipts."""

from __future__ import annotations

from pathlib import Path
import os
from contextlib import ExitStack

from jellyorganize.filesystem.apply import _check_unchanged, _move_file, _release_conflict, _validate_entry
from jellyorganize.filesystem.durable import journal_move, new_receipt, discard_stage
from jellyorganize.filesystem.locking import media_lock
from jellyorganize.filesystem.safety import UnsafePath, relative_file, root_fd, parent_fd, assert_state
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.planning.store import FileState, PlanStore, configured_roots


def _entry(plan, record, config, undo=False):
    source = record.get("original_source") if undo else record.get("source")
    entry = next((entry for entry in plan.entries if str(entry.source) == source), None)
    if entry is None or entry.status != "CONFIRMED":
        raise UnsafePath("recovery item is not confirmed in its saved plan")
    original_root, destination_root = configured_roots(config, entry.kind, plan.workflow)
    if entry.source_root != original_root or entry.destination_root != destination_root:
        raise UnsafePath("recovery roots no longer match configuration")
    if record.get("kind") != entry.kind:
        raise UnsafePath("recovery media type does not match saved plan")
    for file in entry.files:
        relative_file(file.source, entry.source_root)
        relative_file(file.destination, entry.destination_root)
    return entry


def _validate_moves(record, files, source_root, destination_root):
    expected = {str(file.source): file for file in files if file.source != file.destination}
    receipts = record.get("moves", [])
    if not isinstance(receipts, list) or not all(isinstance(move, dict) for move in receipts):
        raise UnsafePath("invalid recovery receipts")
    if len(receipts) != len(expected) or {move.get("source_state", {}).get("source") for move in receipts} != set(expected):
        raise UnsafePath("recovery receipts do not match saved file decisions")
    for receipt in receipts:
        file = expected[receipt["source_state"]["source"]]
        if (receipt["source_state"] != file.model_dump(mode="json") or
                receipt.get("source_root") != str(source_root) or receipt.get("destination_root") != str(destination_root)):
            raise UnsafePath("recovery receipt does not match saved file decisions")
    return expected


def _finish(transaction, record, files, source_root, destination_root, config):
    expected = _validate_moves(record, files, source_root, destination_root)
    completed = []
    for receipt in record["moves"]:
        file = expected[receipt["source_state"]["source"]]
        if receipt.get("rollback") or receipt["phase"] in {"rolled_back", "discarded"}:
            raise UnsafePath("interrupted rollback cannot resume as a forward move")
        with journal_move(transaction, receipt):
            restored = _move_file(file, source_root, destination_root, config)
        completed.append({"source": str(file.source), "destination": str(file.destination),
                          "source_state": file.model_dump(mode="json"),
                          "destination_state": restored.model_dump(mode="json"), "status": "moved"})
    for file in files:
        if file.source == file.destination:
            _check_unchanged(file, source_root)
            completed.append({"source": str(file.source), "destination": str(file.destination),
                              "source_state": file.model_dump(mode="json"), "status": "unchanged"})
    record["files"] = completed


def _undo_states(record, originals):
    """Bind an old undo snapshot to a durably recorded copied rollback.

    The original apply journal can already contain the rollback's new inode
    when a process dies before the undo journal's final status write.
    """
    states = {source: FileState.model_validate(file["destination_state"])
              for source, file in originals.items()}
    for move in record.get("moves", []):
        initial = FileState.model_validate(move["source_state"])
        current = states.get(str(initial.source))
        if current is None:
            raise UnsafePath("undo receipt is absent from its original transaction")
        if initial != current:
            rollback = move.get("rollback", {})
            reverse = FileState.model_validate(rollback.get("source_state", {}))
            if (rollback.get("destination_state") != current.model_dump(mode="json") or
                    rollback.get("phase") != "moved" or
                    reverse.model_dump(mode="json") != move.get("destination_state") or
                    reverse.source != initial.destination or reverse.destination != initial.source or
                    initial.destination != current.destination or initial.size != current.size):
                raise UnsafePath("undo snapshot change has no verified rollback receipt")
            states[str(initial.source)] = initial
    return list(states.values())


def _rollback(transaction, record, files, source_root, destination_root, config):
    expected = _validate_moves(record, files, source_root, destination_root)
    refreshed = {}
    for receipt in reversed(record["moves"]):
        file = expected[receipt["source_state"]["source"]]
        if receipt.get("rollback"):
            rollback = receipt["rollback"]
            state = FileState.model_validate(rollback["source_state"])
            if state.source != file.destination or state.destination != file.source:
                raise UnsafePath("rollback paths do not match saved plan")
            with journal_move(transaction, rollback):
                reverse = _move_file(state, destination_root, source_root, config)
            receipt["phase"] = "rolled_back"
            refreshed[str(file.source)] = reverse
            transaction.write()
        elif receipt["phase"] not in {"discarded", "rolled_back"}:
            try:
                _check_unchanged(file, source_root)
                discard_stage(file, destination_root, transaction, receipt)
                if file.destination.exists() or file.destination.is_symlink():
                    if not receipt.get("destination_state"):
                        raise UnsafePath("existing destination has no ownership receipt")
                    with ExitStack() as stack:
                        descriptor = root_fd(destination_root)
                        stack.callback(os.close, descriptor)
                        parent, name = stack.enter_context(parent_fd(descriptor, relative_file(file.destination, destination_root)))
                        assert_state(parent, name, FileState.model_validate(receipt["destination_state"]))
                        os.unlink(name, dir_fd=parent)
                        os.fsync(parent)
                receipt["phase"] = "rolled_back"
                transaction.write()
            except FileNotFoundError:
                if not receipt.get("destination_state"):
                    raise UnsafePath("missing source has no verified destination receipt")
                state = FileState.model_validate(receipt["destination_state"])
                rollback = receipt["rollback"] = new_receipt(state, destination_root, source_root)
                with journal_move(transaction, rollback):
                    reverse = _move_file(state, destination_root, source_root, config)
                receipt["phase"] = "rolled_back"
                refreshed[str(file.source)] = reverse
                transaction.write()
    record.pop("rollback_error", None)
    return refreshed


def recover_pending(config, root: Path | None = None, *, fail_on_blocked=False):
    """Caller holds the media lock. No provider calls or newly guessed identities."""
    root = root or config.state_dir / "transactions"
    counts = {"RECOVERED": 0, "BLOCKED": 0}
    errors = []
    if not root.exists():
        return counts, errors
    for path in sorted(root.glob("*.json")):
        try:
            transaction = Transaction.load(root, path.stem)
            unfinished = [record for record in transaction.data["items"]
                          if record.get("status") in {"started", "recovery_blocked"} or record.get("rollback_error") or
                          (record.get("status") not in {"applied", "undone", "rolled back"} and
                           any((move.get("rollback") and move.get("phase") != "rolled_back") or
                               move.get("phase") in {"verified", "published", "moved"}
                               for move in record.get("moves", [])))]
            if not unfinished:
                if transaction.data.get("version") == 2 and not transaction.data.get("completed_at"):
                    transaction.complete()
                continue
            if transaction.data.get("version", 1) == 1:
                raise UnsafePath("legacy interrupted journal has no write-ahead receipts; preserve it for inspection")
            plan = PlanStore(root.parent / "plans").load(transaction.data["plan_id"])
            undo = transaction.data.get("operation") == "undo"
            if plan.workflow == "handoff" and not undo:
                from jellyorganize.downloads.handoff import verify_torrent
                partial = any(move.get("phase") in {"verified", "published", "moved"} for record in unfinished
                              for move in record.get("moves", []))
                verify_torrent(plan, config, recovering=partial)
            original = Transaction.load(root, transaction.data["undo_of"]) if undo else None
            for record in unfinished:
                entry = _entry(plan, record, config, undo)
                if undo:
                    applied = next((item for item in original.data["items"] if item.get("source") == str(entry.source)), None)
                    if applied is None:
                        raise UnsafePath("undo recovery has no original applied item")
                    originals = {file["destination"]: file for file in applied["files"] if file.get("status") == "moved"}
                    files = _undo_states(record, originals)
                    source_root, destination_root = entry.destination_root, entry.source_root
                else:
                    files = entry.files
                    source_root, destination_root = entry.source_root, entry.destination_root
                if (record.get("status") not in {"started", "recovery_blocked"} or record.get("rollback_error") or
                        any(move.get("rollback") for move in record.get("moves", []))):
                    refreshed = _rollback(transaction, record, files, source_root, destination_root, config)
                    if undo:
                        for source, state in refreshed.items():
                            originals[source]["destination_state"] = state.model_dump(mode="json")
                        applied["status"] = "applied"
                        applied.pop("undo_transaction_id", None)
                        original.write()
                    record["status"] = "rolled back"
                else:
                    if not record.get("moves") and undo:
                        from jellyorganize.filesystem.undo import _reverse_files
                        reverse = _reverse_files(applied, entry, config, plan.workflow)
                        record["moves"] = [new_receipt(state, source_root, destination_root)
                                           for _, state in reversed(reverse)]
                        transaction.write()
                    if not record.get("moves") and not undo:
                        _validate_entry(entry, config, plan.workflow)
                        ordered = [*files[1:], files[0]]
                        record["moves"] = [new_receipt(file, source_root, destination_root)
                                           for file in ordered if file.source != file.destination]
                        transaction.write()
                    if not undo and entry.destination and not entry.destination.exists() and _release_conflict(entry):
                        raise FileExistsError("another release occupies the recovery destination")
                    _finish(transaction, record, files, source_root, destination_root, config)
                    record["status"] = "undone" if undo else "applied"
                    if undo:
                        applied["status"] = "undone"
                        applied["undo_transaction_id"] = transaction.data["transaction_id"]
                        original.write()
                record.pop("recovery_error", None)
                transaction.write()
                counts["RECOVERED"] += 1
            transaction.complete()
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{path.stem}: {error}")
            counts["BLOCKED"] += 1
            # Preserve every file and the receipt; a later run can retry safely.
    if fail_on_blocked and errors:
        raise UnsafePath("unfinished transactions need attention: " + "; ".join(errors))
    return counts, errors


def recover(config):
    root = config.state_dir / "transactions"
    with media_lock(root):
        return recover_pending(config, root)
