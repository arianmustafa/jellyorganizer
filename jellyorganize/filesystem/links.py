"""Completed link imports, rebuilt once per operation from durable journals."""

from jellyorganize.filesystem.durable import validate_mode
from jellyorganize.filesystem.safety import UnsafePath, relative_file
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.planning.store import FileState, PlanStore, RetainedSource, configured_roots


def identity(state):
    return state.size, state.mtime_ns, state.inode, state.device


class LinkedImports:
    def __init__(self, config):
        self.config = config
        self.packages = {}
        self.relocations = []
        root = config.state_dir / "transactions"
        store = PlanStore(config.state_dir / "plans")
        transactions = [Transaction.load(root, path.stem) for path in root.glob("*.json")]
        if not any(transaction.data.get("transfer_mode") in {"hardlink", "symlink"} for transaction in transactions):
            self.sources, self.destinations = {}, {}
            return
        # Filenames have random suffixes; several operations can share a second.
        transactions.sort(key=lambda transaction: (transaction.data.get("created_at", ""), transaction.data["transaction_id"]))
        for transaction in transactions:
            if transaction.data.get("operation") == "undo":
                plan = store.load(transaction.data["plan_id"])
                if plan.workflow == "audit":
                    validate_mode(transaction, plan)
                    for record in transaction.data["items"]:
                        if record.get("status") != "undone":
                            continue
                        validate_mode(transaction, plan, record)
                        entry = next((entry for entry in plan.entries if str(entry.source) == record.get("original_source")), None)
                        if entry is None:
                            raise UnsafePath("audit undo is absent from its saved plan")
                        for receipt in record.get("moves", []):
                            before = FileState.model_validate(receipt["source_state"])
                            after = FileState.model_validate(receipt["destination_state"])
                            if (receipt.get("phase") != "moved" or receipt.get("transfer_mode") != "move" or
                                    receipt.get("source_root") != str(entry.destination_root) or
                                    receipt.get("destination_root") != str(entry.source_root) or
                                    after.source != before.destination or after.destination != before.source or
                                    not any(file.source == after.source and file.destination == before.source
                                            and file.link == before.link == after.link for file in entry.files)):
                                raise UnsafePath("audit undo relocation does not match saved plan")
                            self.relocations.append((before, after))
                continue
            if transaction.data.get("transfer_mode", "move") not in {"hardlink", "symlink"} and not any(
                    record.get("operation") == "audit" for record in transaction.data["items"]):
                continue
            plan = store.load(transaction.data["plan_id"])
            validate_mode(transaction, plan)
            entries = {str(entry.source): entry for entry in plan.entries}
            for record in transaction.data["items"]:
                if record.get("status") != "applied" and not (plan.workflow == "audit" and record.get("status") == "undone"):
                    if plan.workflow == "audit":
                        entry = entries.get(record.get("source"))
                        if entry is None:
                            raise UnsafePath("audit rollback is absent from its saved plan")
                        for receipt in record.get("moves", []):
                            rollback = receipt.get("rollback", {})
                            if receipt.get("phase") != "rolled_back" or rollback.get("phase") != "moved":
                                continue
                            before = FileState.model_validate(receipt["source_state"])
                            if before.link is None:
                                continue
                            after = FileState.model_validate(rollback["destination_state"])
                            if (before not in entry.files or after.source != before.source or
                                    after.destination != before.destination or after.link != before.link or
                                    rollback.get("source_state") != receipt.get("destination_state") or
                                    rollback.get("source_root") != str(entry.destination_root) or
                                    rollback.get("destination_root") != str(entry.source_root) or
                                    rollback.get("transfer_mode") != "move"):
                                raise UnsafePath("audit rollback relocation does not match saved plan")
                            self.relocations.append((before, after))
                    continue
                validate_mode(transaction, plan, record)
                entry = entries.get(record.get("source"))
                if entry is None or entry.status != "CONFIRMED":
                    raise UnsafePath("completed import is absent from its saved plan")
                if (record.get("operation") != plan.workflow or record.get("kind") != entry.kind or
                        record.get("destination") != str(entry.destination)):
                    raise UnsafePath("completed import item does not match saved plan")
                expected = {str(file.source): file for file in entry.files}
                files = record.get("files", [])
                if len(files) != len(expected) or {file.get("source") for file in files} != set(expected):
                    raise UnsafePath("completed import files do not match saved plan")
                states = []
                receipts = {receipt.get("source_state", {}).get("source"): receipt for receipt in record.get("moves", [])}
                for file in files:
                    planned = expected[file["source"]]
                    if file.get("source_state") != planned.model_dump(mode="json") or file.get("destination") != str(planned.destination):
                        raise UnsafePath("completed import state does not match saved plan")
                    if file.get("status") == "unchanged" and planned.source == planned.destination:
                        continue
                    state = FileState.model_validate(file["destination_state"])
                    if file.get("status") != "moved" or state.source != planned.destination or state.destination != planned.source:
                        raise UnsafePath("completed import has invalid destination state")
                    if plan.transfer_mode in {"hardlink", "symlink"}:
                        receipt = receipts.get(file["source"], {})
                        if (receipt.get("source_state") != file["source_state"] or
                                receipt.get("destination_state") != file["destination_state"] or
                                receipt.get("source_root") != str(entry.source_root) or
                                receipt.get("destination_root") != str(entry.destination_root) or
                                receipt.get("transfer_mode") != plan.transfer_mode or receipt.get("phase") != "moved" or
                                receipt.get("reused", False) != file.get("reused", False)):
                            raise UnsafePath("completed import receipt does not match saved operation")
                    relative_file(planned.source, entry.source_root)
                    relative_file(state.source, entry.destination_root)
                    if plan.transfer_mode == "hardlink" and identity(state) != identity(planned):
                        raise UnsafePath("completed import is not a hard link")
                    if plan.transfer_mode == "symlink":
                        retained = planned.link or RetainedSource(root=entry.source_root,
                                                                 **planned.model_dump(exclude={"destination", "link"}))
                        if state.link != retained:
                            raise UnsafePath("completed import is not the planned symbolic link")
                    if plan.workflow == "audit":
                        self.relocations.append((planned, state))
                    states.append((planned, state))
                if plan.transfer_mode in {"hardlink", "symlink"}:
                    self.packages[entry.source] = (entry, states)
        self.sources = {}
        self.destinations = {}
        for entry, files in self.packages.values():
            for source, state in files:
                state = self.current(state)
                self.sources[(source.source, entry.source_root, entry.destination_root)] = (source, state)
                self.destinations[state.source] = (entry, source, state)

    def current(self, state):
        # Replay each audit once in chronological order, including A -> B -> A.
        for before, after in self.relocations:
            if state.source != before.source or identity(before) != identity(state):
                continue
            if (state.link is None and identity(after) != identity(state)) or after.link != state.link:
                raise UnsafePath("audit relocation severed a tracked link")
            state = after.model_copy(update={"destination": state.destination})
        return state

    def check(self, source, state, source_root, destination_root, *, missing=False):
        from jellyorganize.filesystem.apply import _check_unchanged
        _check_unchanged(source, source_root)
        state = self.current(state)
        relative_file(state.source, destination_root)
        try:
            _check_unchanged(state, destination_root)
        except FileNotFoundError:
            if missing:
                return state, False
            raise
        return state, True

    def package(self, item):
        package = self.packages.get(item.path)
        if package is None:
            return None
        entry, files = package
        if entry.source_root != item.root or configured_roots(self.config, entry.kind, "ingest") != (
                entry.source_root, entry.destination_root):
            raise UnsafePath("tracked link roots no longer match configuration")
        if {file.source for file, _ in files} != {item.path, *item.sidecars}:
            return None  # New/removed sidecars need matching and collision checks again.
        targets = {}
        complete = True
        for source, state in files:
            state, present = self.check(source, state, entry.source_root, entry.destination_root, missing=True)
            targets[source.source] = state.source
            complete = complete and present
        return entry, targets, complete

    def owned(self, file, source_root, destination_root):
        tracked = self.sources.get((file.source, source_root, destination_root))
        if tracked is not None:
            source, state = tracked
            if identity(source) != identity(file):
                return None
            state, present = self.check(source, state, source_root, destination_root, missing=True)
            if state.source == file.destination and present:
                return state
        return None

    def mode(self, source, source_root, destination_root):
        _, state = self.sources[(source, source_root, destination_root)]
        return "symlink" if state.link is not None else "hardlink"

    def protected(self, file):
        tracked = self.destinations.get(file.source)
        if tracked is not None:
            entry, source, state = tracked
            from jellyorganize.filesystem.apply import _check_unchanged
            _check_unchanged(source, entry.source_root)
            if identity(state) != identity(file):
                raise UnsafePath("tracked audit source changed")
            return state.link is None
        return False

    def snapshot(self, path):
        """Admit only symlinks backed by completed, verified import receipts."""
        tracked = self.destinations.get(path)
        if tracked is None or tracked[2].link is None:
            raise UnsafePath(f"untracked symbolic link: {path}")
        entry, source, state = tracked
        state, _ = self.check(source, state, entry.source_root, entry.destination_root)
        return state
