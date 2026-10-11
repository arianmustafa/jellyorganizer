import errno
import json
import os
import time

import pytest

from jellyorganize import cli
from jellyorganize.filesystem import apply, doctor, durable, undo
from jellyorganize.filesystem.links import LinkedImports
from jellyorganize.filesystem.recovery import recover
from jellyorganize.planning.store import PlanStore, SourceState, capture
from test_auto import setup
from test_recovery import preparation, worker


def linked_plan(config, tmp_path, monkeypatch):
    config.filesystem.mode = "symlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    transaction, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1, transaction.data
    return source, sidecar, plan, transaction


def assert_link(source, target):
    assert target.is_symlink()
    assert target.readlink() == source
    assert target.read_bytes() == source.read_bytes()


def test_symlink_media_sidecars_saved_mode_and_undo(config, tmp_path, monkeypatch):
    config.filesystem.mode = "symlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    config.filesystem.mode = "move"
    transaction, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    target = plan.entries[0].destination
    assert_link(source, target)
    assert_link(sidecar, target.with_suffix(".en.srt"))
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNDONE"] == 1
    assert source.is_file() and sidecar.is_file()
    assert not target.is_symlink() and not target.with_suffix(".en.srt").is_symlink()
    assert undo.undo_transaction(transaction.data["transaction_id"], config)[1]["UNTOUCHED"] == 1


def test_symlink_does_not_need_hardlinks(config, tmp_path, monkeypatch):
    def no_hardlinks(*args, **kwargs):
        raise OSError(errno.EXDEV, "different mounts")
    monkeypatch.setattr(durable.os, "link", no_hardlinks)
    source, _, plan, _ = linked_plan(config, tmp_path, monkeypatch)
    assert_link(source, plan.entries[0].destination)


@pytest.mark.parametrize("error", [errno.EPERM, errno.EOPNOTSUPP])
def test_symlink_failure_preserves_originals(config, tmp_path, monkeypatch, error):
    config.filesystem.mode = "symlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError(error, "symlinks unavailable")
    monkeypatch.setattr(durable.os, "symlink", fail)
    _, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["ERROR"] == 1
    assert source.exists() and sidecar.exists()
    assert not plan.entries[0].destination.is_symlink()
    assert not list(config.movies.library.rglob("*.part"))


@pytest.mark.parametrize("change", ["remove_original", "replace_original", "modify_data", "replace_link", "change_target", "symlink_parent"])
def test_symlink_undo_refuses_changed_files(config, tmp_path, monkeypatch, change):
    source, sidecar, plan, transaction = linked_plan(config, tmp_path, monkeypatch)
    target = plan.entries[0].destination
    if change == "remove_original":
        source.unlink()
    elif change == "replace_original":
        source.rename(source.with_suffix(".old"))
        source.write_bytes(b"replacement")
    elif change == "modify_data":
        source.write_bytes(b"changed")
    elif change == "replace_link":
        target.unlink()
        target.write_bytes(b"replacement")
    elif change == "change_target":
        target.unlink()
        target.symlink_to(sidecar)
    else:
        parent = target.parent
        relocated = parent.with_name("relocated")
        parent.rename(relocated)
        parent.symlink_to(relocated, target_is_directory=True)
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["STALE"] + counts["ERROR"] == 1 and counts["UNDONE"] == 0
    assert target.exists() or target.is_symlink()
    assert sidecar.exists() and target.with_suffix(".en.srt").is_symlink()


@pytest.mark.parametrize("operation,stage", [
    *[("apply", stage) for stage in ["intent_written", "stage_created", "stage_recorded", "stage_verified", "published", "source_retained", "move_recorded", "item_committed", "transaction_committed"]],
    *[("undo", stage) for stage in ["intent_written", "link_removed", "move_recorded", "item_committed", "transaction_committed"]],
])
def test_symlink_crash_recovery(config, tmp_path, monkeypatch, operation, stage):
    config.filesystem.mode = "symlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    payload = {"plan_id": plan.plan_id, "operation": operation, "stage": stage}
    if operation == "undo":
        transaction, _ = apply.apply_plan(plan, config, config.state_dir / "transactions")
        payload["transaction_id"] = transaction.data["transaction_id"]
    worker(config, tmp_path, payload)
    config.filesystem.mode = "move"
    assert recover(config)[1] == []
    target = plan.entries[0].destination
    assert source.exists() and sidecar.exists()
    if operation == "apply":
        assert_link(source, target)
        assert_link(sidecar, target.with_suffix(".en.srt"))
    else:
        assert not target.is_symlink() and not target.with_suffix(".en.srt").is_symlink()
    assert recover(config) == ({"RECOVERED": 0, "BLOCKED": 0}, [])


@pytest.mark.parametrize("operation,stage", [("apply", "link_removed"), ("undo", "published"), ("apply", "move_recorded")])
def test_symlink_rollback_crash_recovery(config, tmp_path, monkeypatch, operation, stage):
    config.filesystem.mode = "symlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    payload = {"plan_id": plan.plan_id, "operation": operation, "stage": stage, "rollback": True}
    if operation == "undo":
        transaction, _ = apply.apply_plan(plan, config, config.state_dir / "transactions")
        payload["transaction_id"] = transaction.data["transaction_id"]
    if stage == "move_recorded":
        payload["occurrence"] = 2
    worker(config, tmp_path, payload)
    assert recover(config)[1] == []
    target = plan.entries[0].destination
    assert source.exists() and sidecar.exists()
    if operation == "undo":
        assert_link(source, target)
        assert undo.undo_transaction(transaction.data["transaction_id"], config)[1]["UNDONE"] == 1
    else:
        assert not target.is_symlink() and not target.with_suffix(".en.srt").is_symlink()


def test_symlink_runs_skip_and_repair_only_missing_links(tmp_path, monkeypatch, capsys):
    settings, paths = setup(tmp_path, monkeypatch)
    with settings.open("a") as stream:
        stream.write('[filesystem]\nmode = "symlink"\n')
    source = paths["movie_in"] / "we.live.in.time.2024.mkv"
    source.write_bytes(b"movie")
    source.with_suffix(".en.srt").write_bytes(b"subtitle")
    assert cli.main(["--config", str(settings), "organize", "movies", "--dry-run"]) == 0
    assert "Transfer: SYMLINK (originals retained)" in capsys.readouterr().out
    assert not list(paths["movie_lib"].iterdir())
    assert cli.main(["--config", str(settings), "organize", "movies"]) == 0
    capsys.readouterr()
    target = next(paths["movie_lib"].rglob("*.mkv"))
    assert cli.main(["--config", str(settings), "run", "movies"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["SKIP"] == 1
    target.with_suffix(".en.srt").unlink()
    assert cli.main(["--config", str(settings), "organize", "movies"]) == 0
    output = capsys.readouterr().out
    transaction_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Transaction:"))
    assert_link(source, target)
    assert_link(source.with_suffix(".en.srt"), target.with_suffix(".en.srt"))
    assert cli.main(["--config", str(settings), "undo", transaction_id]) == 0
    assert target.is_symlink() and not target.with_suffix(".en.srt").is_symlink()


@pytest.mark.parametrize("completion", ["stable", "marker", "handoff"])
def test_symlink_download_to_library_and_repeated_import(config, tmp_path, monkeypatch, capsys, completion):
    from test_downloads import prepare, HASH
    from test_auto import FakeTMDb, FakeTVmaze
    from jellyorganize.downloads import handoff
    client = prepare(config, tmp_path, monkeypatch)
    client.state = "uploading"
    config.incoming.completion = completion
    config.incoming.stability_seconds = 300
    config.incoming.minimum_age_seconds = 60
    for path in config.downloads.path.rglob("*"):
        if path.is_file():
            os.utime(path, (time.time() - 3600, time.time() - 3600))
    config.filesystem.mode = "symlink"
    config.movies.library.mkdir()
    config.tv.library.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "TMDbProvider", FakeTMDb)
    monkeypatch.setattr(cli, "TVmazeProvider", FakeTVmaze)
    monkeypatch.setattr(handoff, "QBittorrentClient", lambda _: client)
    assert cli.main(["import-download", "--torrent", HASH]) == 0
    capsys.readouterr()
    targets = list(config.tv.library.rglob("*.mkv"))
    assert len(targets) == 2
    for target in targets:
        assert target.is_symlink() and target.readlink().is_relative_to(config.downloads.path)
        assert not target.readlink().is_symlink()
    assert cli.main(["import-download", "--torrent", HASH]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0])["download"]["status"] == "already handed off"
    summary = json.loads(lines[-1])
    assert summary["counts"]["SKIP"] >= 2 and summary["transaction_id"] is None
    assert summary["counts"]["CONFLICT"] == summary["counts"]["ERROR"] == 0
    from jellyorganize.filesystem.transaction import Transaction
    journals = [Transaction.load(config.state_dir / "transactions", path.stem)
                for path in (config.state_dir / "transactions").glob("*.json")]
    store = PlanStore(config.state_dir / "plans")
    for journal in journals:
        if store.load(journal.data["plan_id"]).workflow == "ingest":
            assert undo.undo_transaction(journal.data["transaction_id"], config)[1]["UNDONE"] == 2
    for journal in journals:
        if store.load(journal.data["plan_id"]).workflow == "handoff":
            assert undo.undo_transaction(journal.data["transaction_id"], config)[1]["UNDONE"] == 1
    assert len(list(config.downloads.path.rglob("*.mkv"))) == 2
    assert not list(config.tv.library.rglob("*.mkv"))
    assert not list(config.incoming.path.rglob("*.mkv"))


def test_symlink_handoff_undo_and_reimport(config, tmp_path, monkeypatch):
    from test_downloads import prepare, HASH
    from jellyorganize.downloads.handoff import import_torrent
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "symlink"
    plan, result = import_torrent(config, HASH, client=client)
    for file in plan.entries[0].files:
        assert_link(file.source, file.destination)
    assert undo.undo_transaction(result["transaction_id"], config)[1]["UNDONE"] == 1
    plan, result = import_torrent(config, HASH, client=client)
    assert result["status"] == "handed off"
    for file in plan.entries[0].files:
        assert_link(file.source, file.destination)


def test_symlink_doctor_and_cleanup(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    def no_hardlinks(*args, **kwargs):
        raise OSError(errno.EXDEV, "different mounts")
    monkeypatch.setattr(doctor.os, "link", no_hardlinks)
    result = doctor.probe(source, destination, "symlink")
    assert result["status"] == "OK" and result["link_supported"]
    assert not list(source.iterdir()) and not list(destination.iterdir())


def test_symlink_doctor_reports_failure(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    def no_symlinks(*args, **kwargs):
        raise OSError(errno.EPERM, "symlinks disabled")
    monkeypatch.setattr(doctor.os, "symlink", no_symlinks)
    assert doctor.probe(source, destination, "symlink")["status"] == "ERROR"
    assert not list(source.iterdir()) and not list(destination.iterdir())


def audit_plan(config, original):
    old = original.entries[0].destination
    target = config.movies.library / "Renamed" / "Renamed.mkv"
    entry = original.entries[0].model_copy(deep=True)
    entry.source_root = config.movies.library
    entry.source, entry.destination = old, target
    links = LinkedImports(config)
    entry.files = [capture(old, target, links), capture(old.with_suffix(".en.srt"), target.with_suffix(".en.srt"), links)]
    entry.source_states = [SourceState.model_validate(file.model_dump()) for file in entry.files]
    plan = original.model_copy(update={"plan_id": original.plan_id + "-a", "workflow": "audit", "transfer_mode": "move", "source_roots": {"movie": config.movies.library}, "entries": [entry]})
    PlanStore(config.state_dir / "plans").save(plan)
    return plan


def test_audit_symlink_relocation_and_undo(config, tmp_path, monkeypatch):
    source, _, original, imported = linked_plan(config, tmp_path, monkeypatch)
    plan = audit_plan(config, original)
    old, target = original.entries[0].destination, plan.entries[0].destination
    transaction, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1, transaction.data
    assert_link(source, target)
    assert LinkedImports(config).owned(original.entries[0].files[0].model_copy(update={"destination": target}), original.entries[0].source_root, original.entries[0].destination_root)
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNDONE"] == 1
    assert_link(source, old)
    assert LinkedImports(config).owned(original.entries[0].files[0], original.entries[0].source_root, original.entries[0].destination_root)

    assert undo.undo_transaction(imported.data["transaction_id"], config)[1]["UNDONE"] == 1
    assert source.exists() and not old.is_symlink()


@pytest.mark.parametrize("operation,stage", [("apply", "published"), ("apply", "source_removed"), ("undo", "published"), ("undo", "source_removed")])
def test_audit_symlink_crash_recovery(config, tmp_path, monkeypatch, operation, stage):
    source, _, original, imported = linked_plan(config, tmp_path, monkeypatch)
    plan = audit_plan(config, original)
    payload = {"plan_id": plan.plan_id, "operation": operation, "stage": stage}
    if operation == "undo":
        transaction, _ = apply.apply_plan(plan, config, config.state_dir / "transactions")
        payload["transaction_id"] = transaction.data["transaction_id"]
    worker(config, tmp_path, payload)
    assert recover(config)[1] == []
    target = original.entries[0].destination if operation == "undo" else plan.entries[0].destination
    assert_link(source, target)
    assert LinkedImports(config).owned(original.entries[0].files[0].model_copy(update={"destination": target}), original.entries[0].source_root, original.entries[0].destination_root)


@pytest.mark.parametrize("operation", ["apply", "undo"])
def test_audit_symlink_failed_operation_tracks_rollback(config, tmp_path, monkeypatch, operation):
    source, _, original, _ = linked_plan(config, tmp_path, monkeypatch)
    plan = audit_plan(config, original)
    module = apply
    if operation == "undo":
        transaction, _ = apply.apply_plan(plan, config, config.state_dir / "transactions")
        module = undo
    move = module._move_file
    def fail_second(file, *args):
        if file.source.suffix == (".srt" if operation == "undo" else ".mkv"):
            raise OSError("injected failure")
        return move(file, *args)
    monkeypatch.setattr(module, "_move_file", fail_second)
    if operation == "apply":
        _, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    else:
        _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["ERROR"] == 1
    assert recover(config)[1] == []
    target = original.entries[0].destination if operation == "apply" else plan.entries[0].destination
    assert_link(source, target)
    links = LinkedImports(config)
    for file in original.entries[0].files:
        destination = target if file.source.suffix == ".mkv" else target.with_suffix(".en.srt")
        assert links.owned(file.model_copy(update={"destination": destination}), original.entries[0].source_root, original.entries[0].destination_root)


def test_audit_relocates_both_symlink_movie_versions(config, tmp_path, monkeypatch):
    from test_new_features import movie_plan
    from test_planning import touch
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config.filesystem.mode = "symlink"
    config.naming.movie_versions = True
    config.movies.library.mkdir()
    for resolution in ("1080p", "2160p"):
        source = config.movies.incoming / resolution / f"Dune.2021.{resolution}.mkv"
        touch(source)
        touch(source.with_suffix(".en.srt"))
    original = movie_plan(config, tmp_path)
    store = PlanStore(config.state_dir / "plans")
    store.save(original)
    imported, counts = apply.apply_plan(original, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 2
    links = LinkedImports(config)
    entries = []
    for old in original.entries:
        entry = old.model_copy(deep=True)
        entry.source_root = config.movies.library
        entry.source = old.destination
        entry.destination = config.movies.library / "Renamed (2021) [tmdbid-438631]" / old.destination.name.replace("Dune", "Renamed")
        entry.files = [capture(file.destination, entry.destination.with_name(file.destination.name.replace("Dune", "Renamed")), links)
                       for file in old.files]
        entry.source_states = [SourceState.model_validate(file.model_dump()) for file in entry.files]
        entries.append(entry)
    audit = original.model_copy(update={"plan_id": original.plan_id + "-a", "workflow": "audit", "transfer_mode": "move",
                                       "source_roots": {"movie": config.movies.library}, "entries": entries})
    store.save(audit)
    relocated, counts = apply.apply_plan(audit, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 2, relocated.data
    for old, entry in zip(original.entries, audit.entries):
        for retained, moved in zip(old.files, entry.files):
            assert_link(retained.source, moved.destination)
    assert undo.undo_transaction(relocated.data["transaction_id"], config)[1]["UNDONE"] == 2
    for entry in original.entries:
        for file in entry.files:
            assert_link(file.source, file.destination)
    assert undo.undo_transaction(imported.data["transaction_id"], config)[1]["UNDONE"] == 2
    assert all(file.source.exists() and not file.destination.is_symlink() for entry in original.entries for file in entry.files)


def test_symlink_movie_versions_and_review(config, tmp_path, monkeypatch):
    import asyncio
    from test_new_features import movie_plan
    from test_planning import touch, FakeTMDb
    from jellyorganize.planning.audit import plan_audit
    from jellyorganize.scanner.library import scan_library
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config.filesystem.mode = "symlink"
    config.naming.movie_versions = True
    config.movies.library.mkdir()
    for resolution in ("1080p", "2160p"):
        source = config.movies.incoming / resolution / f"Dune.2021.{resolution}.mkv"
        touch(source)
        touch(source.with_suffix(".en.srt"))
    plan = movie_plan(config, tmp_path)
    PlanStore(config.state_dir / "plans").save(plan)
    transaction, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 2, transaction.data
    scan = scan_library(config, "movie")
    assert len(scan.items) == 2
    assert all(proposal.status == "SKIP" for proposal in asyncio.run(plan_audit(scan, config, FakeTMDb())))
    audit = audit_plan(config, plan)
    item = next(item for item in scan.items if item.path == audit.entries[0].source)
    assert cli.review_source_current(audit.entries[0], item)
    plan.entries[0].source.write_bytes(b"changed target")
    assert not cli.review_source_current(audit.entries[0], item)
