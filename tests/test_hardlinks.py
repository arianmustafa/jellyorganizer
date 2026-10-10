import errno
import json
import os

import pytest

from jellyorganize import cli
from jellyorganize.config import load_config
from jellyorganize.filesystem import apply, durable, undo
from jellyorganize.filesystem.recovery import recover
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.planning.store import PlanStore, capture, SourceState
from test_apply import confirmed_plan, touch
from test_auto import setup
from test_recovery import preparation, worker


def linked_plan(config, tmp_path, monkeypatch):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    transaction, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    return source, sidecar, plan, transaction


def same_file(source, target):
    assert source.is_file() and target.is_file()
    assert (source.stat().st_dev, source.stat().st_ino) == (target.stat().st_dev, target.stat().st_ino)


def test_hardlink_media_sidecars_and_undo_keep_originals(config, tmp_path, monkeypatch):
    source, sidecar, plan, transaction = linked_plan(config, tmp_path, monkeypatch)
    target = plan.entries[0].destination
    same_file(source, target)
    same_file(sidecar, target.with_suffix(".en.srt"))
    config.filesystem.mode = "move"
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNDONE"] == 1
    assert source.read_bytes() == b"movie-data" * 256000 and sidecar.exists()
    assert not target.exists() and not target.with_suffix(".en.srt").exists()
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNTOUCHED"] == 1


@pytest.mark.parametrize("error", [errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP])
def test_hardlink_never_falls_back_to_copy(config, tmp_path, monkeypatch, error):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError(error, "link unavailable")
    monkeypatch.setattr(durable.os, "link", fail)
    _, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["ERROR"] == 1
    assert source.exists() and sidecar.exists()
    assert not plan.entries[0].destination.exists()
    assert not list(config.movies.library.rglob("*.part"))


@pytest.mark.parametrize("failure", ["remove_original", "replace_original", "modify_data", "replace_link", "symlink", "receipt_mode"])
def test_hardlink_undo_preserves_unverified_data(config, tmp_path, monkeypatch, failure):
    source, sidecar, plan, transaction = linked_plan(config, tmp_path, monkeypatch)
    target = plan.entries[0].destination
    if failure == "remove_original":
        source.unlink()
    elif failure == "replace_original":
        source.unlink()
        source.write_bytes(b"replacement")
    elif failure == "modify_data":
        source.write_bytes(b"changed shared bytes")
    elif failure == "replace_link":
        target.unlink()
        target.write_bytes(b"replacement")
    elif failure == "symlink":
        target.unlink()
        target.symlink_to(source)
    else:
        transaction.data["items"][0]["transfer_mode"] = "move"
        transaction.write()
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["STALE"] == 1 and counts["UNDONE"] == 0
    assert target.exists() and target.with_suffix(".en.srt").exists() and sidecar.exists()


@pytest.mark.parametrize("operation,stages", [
    ("apply", ["intent_written", "stage_created", "stage_recorded", "stage_verified", "published",
               "source_retained", "move_recorded", "item_committed", "transaction_committed"]),
    ("undo", ["intent_written", "link_removed", "move_recorded", "item_committed", "transaction_committed"]),
])
def test_hardlink_crash_recovery(config, tmp_path, monkeypatch, operation, stages):
    # Each checkpoint uses independent media/state directories.
    for index, stage in enumerate(stages):
        case = tmp_path / str(index)
        case.mkdir()
        settings = config.model_copy(deep=True)
        settings.movies.incoming = case / "Incoming"
        settings.movies.library = case / "Movies"
        settings.filesystem.mode = "hardlink"
        source, sidecar, plan = preparation(settings, case, monkeypatch)
        payload = {"plan_id": plan.plan_id, "operation": operation, "stage": stage}
        if operation == "undo":
            original, _ = apply.apply_plan(plan, settings, settings.state_dir / "transactions")
            payload["transaction_id"] = original.data["transaction_id"]
        worker(settings, case, payload)
        settings.filesystem.mode = "move"  # Recovery must use persisted decisions.
        _, errors = recover(settings)
        assert not errors, (stage, errors)
        target = plan.entries[0].destination
        assert source.exists() and sidecar.exists()
        if operation == "apply":
            same_file(source, target)
            same_file(sidecar, target.with_suffix(".en.srt"))
        else:
            assert not target.exists() and not target.with_suffix(".en.srt").exists()
        counts, errors = recover(settings)
        assert counts == {"RECOVERED": 0, "BLOCKED": 0} and not errors


def test_link_apply_partial_failure_rolls_back_only_created_links(config, tmp_path, monkeypatch):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    original = apply._move_file
    def fail_media(file, *args):
        if file.source.suffix == ".mkv":
            raise OSError("media failure")
        return original(file, *args)
    monkeypatch.setattr(apply, "_move_file", fail_media)
    _, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["ERROR"] == 1
    assert source.exists() and sidecar.exists()
    assert not plan.entries[0].destination.with_suffix(".en.srt").exists()
    assert recover(config)[1] == []


@pytest.mark.parametrize("operation,stage", [("apply", "link_removed"), ("undo", "published")])
def test_link_rollback_crash_recovery(config, tmp_path, monkeypatch, operation, stage):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    payload = {"plan_id": plan.plan_id, "operation": operation, "stage": stage, "rollback": True}
    if operation == "undo":
        original, _ = apply.apply_plan(plan, config, config.state_dir / "transactions")
        payload["transaction_id"] = original.data["transaction_id"]
    worker(config, tmp_path, payload)
    _, errors = recover(config)
    assert not errors, errors
    assert source.exists() and sidecar.exists()
    target = plan.entries[0].destination
    if operation == "undo":
        same_file(source, target)
        same_file(sidecar, target.with_suffix(".en.srt"))
    else:
        assert not target.exists() and not target.with_suffix(".en.srt").exists()


def cli_setup(tmp_path, monkeypatch):
    settings, paths = setup(tmp_path, monkeypatch)
    with settings.open("a") as stream:
        stream.write('[filesystem]\nmode = "hardlink"\n')
    source = paths["movie_in"] / "we.live.in.time.2024.mkv"
    source.write_bytes(b"movie")
    source.with_suffix(".en.srt").write_bytes(b"subtitle")
    return settings, paths, source


def test_link_runs_skip_existing_and_repair_only_missing_links(tmp_path, monkeypatch, capsys):
    settings, paths, source = cli_setup(tmp_path, monkeypatch)
    assert cli.main(["--config", str(settings), "organize", "movies"]) == 0
    capsys.readouterr()
    target = next(paths["movie_lib"].rglob("*.mkv"))
    assert cli.main(["--config", str(settings), "run", "movies"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["SKIP"] == 1 and summary["transaction_id"] is None
    target.with_suffix(".en.srt").unlink()
    assert cli.main(["--config", str(settings), "organize", "movies"]) == 0
    output = capsys.readouterr().out
    transaction_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Transaction:"))
    same_file(source, target)
    same_file(source.with_suffix(".en.srt"), target.with_suffix(".en.srt"))
    assert cli.main(["--config", str(settings), "undo", transaction_id]) == 0
    capsys.readouterr()
    assert target.exists()  # The repair transaction did not create the media link.
    assert not target.with_suffix(".en.srt").exists()


def test_hardlink_dry_run_creates_no_library_directories(tmp_path, monkeypatch, capsys):
    settings, paths, source = cli_setup(tmp_path, monkeypatch)
    assert cli.main(["--config", str(settings), "organize", "movies", "--dry-run"]) == 0
    assert "Transfer: HARDLINK (originals retained)" in capsys.readouterr().out
    assert source.exists() and not list(paths["movie_lib"].iterdir())


def audit_plan(config, original, target):
    entry = original.entries[0].model_copy(deep=True)
    entry.source_root = config.movies.library
    entry.source = original.entries[0].destination
    entry.destination = target
    entry.files = [capture(entry.source, target), capture(entry.source.with_suffix(".en.srt"), target.with_suffix(".en.srt"))]
    entry.source_states = [SourceState.model_validate(file.model_dump()) for file in entry.files]
    plan = original.model_copy(update={"plan_id": original.plan_id + "-a", "workflow": "audit", "transfer_mode": "move",
                                      "source_roots": {"movie": config.movies.library}, "entries": [entry]})
    return PlanStore(config.state_dir / "plans").save(plan)


def test_audit_relocation_and_undo_update_link_lookup(tmp_path, monkeypatch, capsys):
    settings, paths, source = cli_setup(tmp_path, monkeypatch)
    assert cli.main(["--config", str(settings), "organize", "movies"]) == 0
    capsys.readouterr()
    config = load_config(settings)
    original = PlanStore(config.state_dir / "plans").load(next((config.state_dir / "plans").glob("*.json")).stem)
    old = original.entries[0].destination
    target = paths["movie_lib"] / "Renamed" / "Renamed.mkv"
    plan = audit_plan(config, original, target)
    transaction, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    assert cli.main(["--config", str(settings), "run", "movies"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["SKIP"] == 1
    assert not old.exists()
    same_file(source, target)
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNDONE"] == 1
    assert cli.main(["--config", str(settings), "run", "movies"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["SKIP"] == 1
    same_file(source, old)


def test_audit_cannot_copy_tracked_links_when_link_syscall_fails(config, tmp_path, monkeypatch):
    source, sidecar, original, _ = linked_plan(config, tmp_path, monkeypatch)
    plan = audit_plan(config, original, config.movies.library / "Renamed" / "Renamed.mkv")
    def fail(*args, **kwargs):
        raise OSError(errno.EXDEV, "incompatible mount")
    monkeypatch.setattr(durable.os, "link", fail)
    _, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["ERROR"] == 1
    same_file(source, original.entries[0].destination)
    assert not plan.entries[0].destination.exists()


def test_audit_undo_recovery_without_receipts_preserves_hardlinks(config, tmp_path, monkeypatch):
    source, sidecar, original, _ = linked_plan(config, tmp_path, monkeypatch)
    plan = audit_plan(config, original, config.movies.library / "Renamed" / "Renamed.mkv")
    applied, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    pending = Transaction(config.state_dir / "transactions", plan.plan_id)
    pending.data.update(operation="undo", undo_of=applied.data["transaction_id"])
    entry = plan.entries[0]
    pending.append({"source": str(entry.destination), "destination": str(entry.source),
                    "original_source": str(entry.source), "kind": entry.kind,
                    "status": "started", "files": [], "moves": [], "transfer_mode": "move"})
    original_link = durable.os.link
    def fail(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "link unavailable")
    monkeypatch.setattr(durable.os, "link", fail)
    counts, errors = recover(config)
    assert counts["BLOCKED"] == 1 and errors
    same_file(source, entry.destination)
    assert not entry.source.exists()
    monkeypatch.setattr(durable.os, "link", original_link)
    counts, errors = recover(config)
    assert counts["RECOVERED"] == 1 and not errors
    same_file(source, entry.source)
    same_file(sidecar, entry.source.with_suffix(".en.srt"))


def test_completed_downloader_link_rejects_changed_selection(config, tmp_path, monkeypatch):
    from test_downloads import prepare, HASH
    from jellyorganize.downloads.handoff import import_torrent
    from jellyorganize.downloads.qbittorrent import DownloadError
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "hardlink"
    client.files[1]["priority"] = 0
    _, result = import_torrent(config, HASH, client=client)
    assert result["status"] == "handed off"
    subtitle = config.incoming.path / client.files[1]["name"]
    assert not subtitle.exists()
    client.files[1]["priority"] = 1
    with pytest.raises(DownloadError, match="file selection changed"):
        import_torrent(config, HASH, client=client)
    assert not subtitle.exists()
    assert (config.downloads.path / client.files[1]["name"]).read_bytes() == b"subtitle"


def test_downloader_hardlinks_and_repeat_after_undo(config, tmp_path, monkeypatch):
    from test_downloads import prepare, HASH
    from jellyorganize.downloads.handoff import import_torrent
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "hardlink"
    plan, result = import_torrent(config, HASH, client=client)
    for file in plan.entries[0].files:
        same_file(file.source, file.destination)
    assert import_torrent(config, HASH, client=client)[1]["status"] == "already handed off"
    _, counts = undo.undo_transaction(result["transaction_id"], config)
    assert counts["UNDONE"] == 1
    plan, result = import_torrent(config, HASH, client=client)
    assert result["status"] == "handed off"
    for file in plan.entries[0].files:
        same_file(file.source, file.destination)


def test_legacy_plan_still_moves_after_enabling_links(config, tmp_path, monkeypatch):
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    data = plan.model_dump(mode="json")
    data["version"] = 1
    del data["transfer_mode"]
    path = config.state_dir / "plans" / f"{plan.plan_id}.json"
    path.chmod(0o600)
    path.write_text(json.dumps(data))
    config.filesystem.mode = "hardlink"
    saved = PlanStore(config.state_dir / "plans").load(plan.plan_id)
    transaction, counts = apply.apply_plan(saved, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1 and not source.exists()
    _, counts = undo.undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNDONE"] == 1 and source.exists() and sidecar.exists()


def test_downloader_to_library_retains_every_original_and_is_idempotent(config, tmp_path, monkeypatch, capsys):
    from test_downloads import prepare, HASH
    from test_auto import FakeTMDb, FakeTVmaze
    from jellyorganize.downloads import handoff
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "hardlink"
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
    assert len(list(config.tv.library.rglob("*.mkv"))) == 2
    for target in config.tv.library.rglob("*.mkv"):
        assert any(os.path.samefile(source, target) for source in config.downloads.path.rglob("*.mkv"))
        assert any(os.path.samefile(source, target) for source in config.incoming.path.rglob("*.mkv"))
    assert len(list(config.downloads.path.rglob("*.mkv"))) == 2
    assert len(list(config.incoming.path.rglob("*.mkv"))) == 2
    assert cli.main(["import-download", "--torrent", HASH]) == 0
    output = capsys.readouterr().out.splitlines()
    assert json.loads(output[0])["download"]["status"] == "already handed off"
    summary = json.loads(output[-1])
    assert summary["counts"]["SKIP"] >= 2 and summary["transaction_id"] is None
    assert summary["counts"]["CONFLICT"] == summary["counts"]["ERROR"] == 0


def test_downloader_recognizes_linked_journal_after_index_update_crash(config, tmp_path, monkeypatch):
    import sqlite3
    from test_downloads import prepare, HASH
    from jellyorganize.downloads.handoff import import_torrent
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "hardlink"
    plan, result = import_torrent(config, HASH, client=client)
    with sqlite3.connect(config.state_dir / "downloads.sqlite3") as connection:
        connection.execute("UPDATE handoffs SET status='planned', transaction_id=NULL")
    repeated, result = import_torrent(config, HASH, client=client)
    for file in repeated.entries[0].files:
        same_file(file.source, file.destination)
    assert result["status"] == "already handed off" and repeated.plan_id == plan.plan_id
    assert len(list((config.state_dir / "transactions").glob("*.json"))) == 1


def test_downloader_dry_run_and_missing_link_repair(config, tmp_path, monkeypatch):
    from test_downloads import prepare, HASH
    from jellyorganize.downloads.handoff import import_torrent
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "hardlink"
    _, result = import_torrent(config, HASH, dry_run=True, client=client)
    assert result["status"] == "dry run; untouched"
    assert not list(config.incoming.path.iterdir())
    plan, result = import_torrent(config, HASH, client=client)
    missing = plan.entries[0].files[-1]
    missing.destination.unlink()
    repaired, result = import_torrent(config, HASH, client=client)
    for file in repaired.entries[0].files:
        same_file(file.source, file.destination)


def test_foreign_hardlink_at_library_target_is_a_conflict(tmp_path, monkeypatch, capsys):
    settings, paths, source = cli_setup(tmp_path, monkeypatch)
    target = paths["movie_lib"] / "We Live in Time (2024) [tmdbid-1100099]" / "We Live in Time (2024).mkv"
    target.parent.mkdir()
    os.link(source, target)
    assert cli.main(["--config", str(settings), "organize", "movies"]) == 3
    capsys.readouterr()
    same_file(source, target)
    config = load_config(settings)
    assert not list((config.state_dir / "transactions").glob("*.json"))


def test_audit_return_to_previous_filename_is_not_a_mapping_cycle(config, tmp_path, monkeypatch):
    from jellyorganize.filesystem.links import LinkedImports
    from jellyorganize.scanner.incoming import scan_incoming
    source, sidecar, original, _ = linked_plan(config, tmp_path, monkeypatch)
    old = original.entries[0].destination
    first = audit_plan(config, original, config.movies.library / "Renamed" / "Renamed.mkv")
    _, counts = apply.apply_plan(first, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    # A second audit deliberately moves it back while both journals stay applied.
    second = audit_plan(config, first, old)
    _, counts = apply.apply_plan(second, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    item = next(item for item in scan_incoming(config, "movie").items if item.path == source)
    assert LinkedImports(config).package(item)[2] is True
    same_file(source, old)


def test_recovery_rejects_forged_link_receipt_mode(config, tmp_path, monkeypatch):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    worker(config, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "stage_verified"})
    path = next((config.state_dir / "transactions").glob("*.json"))
    transaction = Transaction.load(path.parent, path.stem)
    transaction.data["items"][0]["moves"][0]["transfer_mode"] = "move"
    transaction.write()
    counts, errors = recover(config)
    assert counts["BLOCKED"] == 1 and errors
    assert source.exists() and sidecar.exists()


def test_saved_hardlink_plan_ignores_later_config_mode(config, tmp_path, monkeypatch):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    config.filesystem.mode = "move"
    _, counts = apply.apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    same_file(source, plan.entries[0].destination)


def test_rollback_recovery_discards_owned_unpublished_link_stage(config, tmp_path, monkeypatch):
    config.filesystem.mode = "hardlink"
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    worker(config, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "link_removed",
                             "publication_failure": True})
    assert list(config.movies.library.rglob("*.part"))
    _, errors = recover(config)
    assert not errors, errors
    assert source.exists() and sidecar.exists()
    assert not plan.entries[0].destination.exists()
    assert not list(config.movies.library.rglob("*.part"))
