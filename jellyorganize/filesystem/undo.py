"""Restore applied files using their recorded state and the original saved plan."""

from pathlib import Path

from jellyorganize.config import Config
from jellyorganize.filesystem.apply import _check_unchanged, _move_file
from jellyorganize.filesystem.locking import media_lock
from jellyorganize.filesystem.safety import UnsafePath, relative_file
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.planning.store import FileState, PlanStore, configured_roots
from jellyorganize.filesystem.durable import journal_move, new_receipt, discard_stage, checkpoint, validate_mode


def _reverse_files(record, entry, config, workflow, mode="move"):
    if entry.status != "CONFIRMED":
        raise UnsafePath("transaction item was not confirmed in saved plan")
    source_root, destination_root = configured_roots(config, entry.kind, workflow)
    if entry.source_root != source_root or entry.destination_root != destination_root:
        raise UnsafePath("plan roots no longer match configuration")
    if (record.get("kind") != entry.kind or record.get("destination") != str(entry.destination) or
            record.get("operation") != workflow):
        raise UnsafePath("transaction item does not match saved plan")
    files = record.get("files", [])
    expected = {str(file.source): file for file in entry.files}
    if (not isinstance(files, list) or not all(isinstance(file, dict) for file in files) or
            len(files) != len(expected) or {file.get("source") for file in files} != set(expected)):
        raise UnsafePath("transaction files do not match saved plan")
    reverse = []
    for file in files:
        planned = expected[file["source"]]
        relative_file(planned.source, entry.source_root)
        relative_file(planned.destination, entry.destination_root)
        if file.get("source_state") != planned.model_dump(mode="json") or file.get("destination") != str(planned.destination):
            raise UnsafePath("transaction file does not match saved plan")
        if planned.source == planned.destination and file.get("status") == "unchanged":
            _check_unchanged(planned, entry.source_root)
            continue
        if file.get("status") != "moved" or not isinstance(file.get("destination_state"), dict):
            raise UnsafePath("transaction has no completed move state")
        state = FileState.model_validate(file["destination_state"])
        if state.source != planned.destination or state.destination != planned.source or state.size != planned.size:
            raise UnsafePath("transaction reverse paths do not match saved plan")
        _check_unchanged(state, entry.destination_root)
        if mode == "hardlink":
            _check_unchanged(planned, entry.source_root)
            if (state.device, state.inode, state.mtime_ns) != (planned.device, planned.inode, planned.mtime_ns):
                raise UnsafePath("undo destination is not the recorded hard link")
            if file.get("reused"):
                continue
        elif state.destination.exists() or state.destination.is_symlink():
            raise FileExistsError(f"original path is occupied: {state.destination}")
        reverse.append((file, state))
    return reverse


def undo_transaction(transaction_id: str, config: Config) -> tuple[Transaction, dict[str, int]]:
    root = config.state_dir / "transactions"
    with media_lock(root):
        from jellyorganize.filesystem.recovery import recover_pending
        recover_pending(config, root, fail_on_blocked=True)
        original = Transaction.load(root, transaction_id)
        if original.data.get("operation") == "undo":
            raise ValueError("use the original apply transaction ID for undo")
        plan = PlanStore(config.state_dir / "plans").load(original.data["plan_id"])
        mode = validate_mode(original, plan)
        entries = {str(entry.source): entry for entry in plan.entries}
        transaction = Transaction(root, plan.plan_id)
        transaction.data.update(operation="undo", undo_of=transaction_id, transfer_mode=mode)
        transaction.write()
        counts = {"UNDONE": 0, "STALE": 0, "CONFLICT": 0, "ERROR": 0, "UNTOUCHED": 0}
        for record in reversed(original.data["items"]):
            if record.get("status") != "applied":
                counts["UNTOUCHED"] += 1
                continue
            result = {"source": record.get("destination"), "destination": record.get("source"),
                      "original_source": record.get("source"), "kind": record.get("kind"),
                      "status": "started", "files": [], "moves": [], "transfer_mode": mode}
            transaction.append(result)
            moved = []
            try:
                entry = entries.get(record.get("source"))
                if entry is None:
                    raise UnsafePath("transaction source is absent from saved plan")
                validate_mode(original, plan, record)
                reverse = _reverse_files(record, entry, config, plan.workflow, mode)
                from jellyorganize.filesystem.links import LinkedImports
                links = LinkedImports(config)
                result["moves"] = [new_receipt(state, entry.destination_root, entry.source_root,
                                              "unlink" if mode == "hardlink" else "move",
                                              link_only=plan.workflow == "audit" and links.protected(state))
                                   for _, state in reversed(reverse)]
                transaction.write()
                # Move the media back first, then its sidecars, reversing apply order.
                for file, state in reversed(reverse):
                    receipt = next(move for move in result["moves"] if move["source_state"]["source"] == str(state.source))
                    with journal_move(transaction, receipt):
                        restored = _move_file(state, entry.destination_root, entry.source_root, config)
                    moved.append((file, restored))
                    result["files"].append({"source": str(state.source), "destination": str(state.destination),
                                            "destination_state": restored.model_dump(mode="json"), "status": "moved"})
                    transaction.write()
                result["status"] = "undone"
                record["status"] = "undone"
                record["undo_transaction_id"] = transaction.data["transaction_id"]
                original.write()
                transaction.write()
                checkpoint("item_committed")
                counts["UNDONE"] += 1
            except (UnsafePath, FileNotFoundError) as error:
                result["status"] = "stale"
                result["error"] = str(error)
                counts["STALE"] += 1
            except FileExistsError as error:
                result["status"] = "conflict"
                result["error"] = str(error)
                counts["CONFLICT"] += 1
            except (OSError, ValueError) as error:
                result["status"] = "error"
                result["error"] = str(error)
                counts["ERROR"] += 1
            finally:
                if result["status"] != "undone" and moved:
                    record["status"] = "applied"
                    record.pop("undo_transaction_id", None)
                    for file, state in reversed(moved):
                        try:
                            receipt = next(move for move in result["moves"] if move["source_state"]["source"] == str(state.destination))
                            rollback = receipt["rollback"] = new_receipt(state, entry.source_root, entry.destination_root,
                                                                         "hardlink" if mode == "hardlink" else "move",
                                                                         link_only=receipt.get("link_only", False))
                            with journal_move(transaction, rollback):
                                refreshed = _move_file(state, entry.source_root, entry.destination_root, config)
                            receipt["phase"] = "rolled_back"
                            file["destination_state"] = refreshed.model_dump(mode="json")
                            result["files"].append({"source": str(state.source), "destination": str(state.destination), "status": "rolled back"})
                        except (OSError, ValueError) as error:
                            result["status"] = "error"
                            result["rollback_error"] = str(error)
                            counts["ERROR"] += 1
                    original.write()
                    checkpoint("rollback_original_committed")
                if result["status"] != "undone":
                    for receipt in result["moves"]:
                        state = FileState.model_validate(receipt["source_state"])
                        try:
                            _check_unchanged(state, entry.destination_root)
                            discard_stage(state, entry.source_root, transaction, receipt)
                        except (OSError, ValueError):
                            pass
                transaction.write()
        transaction.complete()
        checkpoint("transaction_committed")
        return transaction, counts
