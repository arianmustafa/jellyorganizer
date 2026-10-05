import pytest

from jellyorganize.config import load_config
from jellyorganize.filesystem import undo
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.filesystem.locking import media_lock
from jellyorganize.filesystem.transaction import Transaction
from test_auto import setup
from jellyorganize import cli


def applied_movie(tmp_path, monkeypatch, capsys):
    config_path, paths = setup(tmp_path, monkeypatch)
    source = paths["movie_in"] / "we.live.in.time.2024.mkv"
    source.write_bytes(b"movie")
    source.with_suffix(".en.srt").write_bytes(b"subtitle")
    assert cli.main(["--config", str(config_path), "organize", "movies"]) == 0
    output = capsys.readouterr().out
    transaction_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Transaction:"))
    config = load_config(config_path)
    original = Transaction.load(config.state_dir / "transactions", transaction_id)
    target = paths["movie_lib"] / "We Live in Time (2024) [tmdbid-1100099]" / "We Live in Time (2024).mkv"
    return config, original, source, target


@pytest.mark.parametrize("failure", ["changed_media", "changed_sidecar", "occupied_media", "occupied_sidecar", "symlink", "changed_config"])
def test_undo_refuses_modified_or_occupied_files_without_partial_moves(tmp_path, monkeypatch, capsys, failure):
    config, original, source, target = applied_movie(tmp_path, monkeypatch, capsys)
    if failure == "changed_media":
        target.write_bytes(b"modified movie")
    elif failure == "changed_sidecar":
        target.with_suffix(".en.srt").write_bytes(b"modified subtitle")
    elif failure == "occupied_media":
        source.write_bytes(b"new download")
    elif failure == "occupied_sidecar":
        source.with_suffix(".en.srt").write_bytes(b"new subtitle")
    elif failure == "symlink":
        original_target = target.with_suffix(".backup")
        target.rename(original_target)
        target.symlink_to(original_target)
    elif failure == "changed_config":
        config.movies.library = tmp_path / "Different Library"
    _, counts = undo.undo_transaction(original.data["transaction_id"], config)
    assert counts["UNDONE"] == 0
    assert counts["CONFLICT" if failure.startswith("occupied") else "STALE"] == 1
    assert target.exists() and target.with_suffix(".en.srt").exists()
    assert source.exists() == (failure == "occupied_media")
    assert source.with_suffix(".en.srt").exists() == (failure == "occupied_sidecar")


def test_undo_rolls_back_media_when_sidecar_restore_fails_and_can_retry(tmp_path, monkeypatch, capsys):
    config, original, source, target = applied_movie(tmp_path, monkeypatch, capsys)
    move = undo._move_file

    def fail_sidecar(file, source_root, destination_root, config):
        if file.source.suffix == ".srt":
            raise OSError("simulated sidecar failure")
        return move(file, source_root, destination_root, config)

    monkeypatch.setattr(undo, "_move_file", fail_sidecar)
    _, counts = undo.undo_transaction(original.data["transaction_id"], config)
    assert counts["ERROR"] == 1 and counts["UNDONE"] == 0
    assert target.read_bytes() == b"movie" and target.with_suffix(".en.srt").exists()
    assert not source.exists()
    monkeypatch.setattr(undo, "_move_file", move)
    _, counts = undo.undo_transaction(original.data["transaction_id"], config)
    assert counts["UNDONE"] == 1
    assert source.read_bytes() == b"movie" and source.with_suffix(".en.srt").read_bytes() == b"subtitle"


@pytest.mark.parametrize("tampering", ["escape", "state", "omit_file"])
def test_undo_checks_journal_against_immutable_plan(tmp_path, monkeypatch, capsys, tampering):
    config, original, source, target = applied_movie(tmp_path, monkeypatch, capsys)
    files = original.data["items"][0]["files"]
    if tampering == "escape":
        files[0]["destination_state"]["destination"] = str(tmp_path / "outside.srt")
    elif tampering == "state":
        files[0]["source_state"]["size"] = 999
    else:
        files.pop()
    original.write()
    _, counts = undo.undo_transaction(original.data["transaction_id"], config)
    assert counts["STALE"] == 1 and counts["UNDONE"] == 0
    assert target.exists() and not source.exists()


def test_apply_and_undo_share_a_nonblocking_lock(tmp_path, monkeypatch, capsys):
    from jellyorganize.planning.store import PlanStore

    config, original, source, target = applied_movie(tmp_path, monkeypatch, capsys)
    root = config.state_dir / "transactions"
    plan = PlanStore(config.state_dir / "plans").load(original.data["plan_id"])
    with media_lock(root):
        with pytest.raises(ValueError, match="another apply or undo"):
            undo.undo_transaction(original.data["transaction_id"], config)
        with pytest.raises(ValueError, match="another apply or undo"):
            apply_plan(plan, config, root)
    assert target.exists() and not source.exists()


def test_undo_continues_with_unrelated_safe_items(tmp_path, monkeypatch, capsys):
    config_path, paths = setup(tmp_path, monkeypatch)
    movie = paths["movie_in"] / "we.live.in.time.2024.mkv"
    episode = paths["tv_in"] / "The Bear (2022)" / "The.Bear.S02E03.mkv"
    episode.parent.mkdir(parents=True)
    movie.write_bytes(b"movie")
    episode.write_bytes(b"episode")
    assert cli.main(["--config", str(config_path), "organize"]) == 0
    output = capsys.readouterr().out
    transaction_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Transaction:"))
    movie.write_bytes(b"new download")
    assert cli.main(["--config", str(config_path), "undo", transaction_id]) == 3
    output = capsys.readouterr().out
    assert "UNDONE: 1" in output and "CONFLICT: 1" in output
    assert episode.read_bytes() == b"episode" and movie.read_bytes() == b"new download"


def test_undo_rejects_path_traversal(tmp_path, monkeypatch, capsys):
    config_path, _ = setup(tmp_path, monkeypatch)
    assert cli.main(["--config", str(config_path), "undo", "../../outside"]) == 1
    assert "invalid transaction ID" in capsys.readouterr().err


def test_cross_filesystem_undo_preserves_contents_and_can_retry_after_rollback(tmp_path, monkeypatch, capsys):
    import errno
    from jellyorganize.filesystem import apply

    config, original, source, target = applied_movie(tmp_path, monkeypatch, capsys)
    initial_inode = target.stat().st_ino

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "simulate cross-filesystem moves")

    monkeypatch.setattr(apply.os, "link", cross_device)
    move = undo._move_file

    def fail_sidecar(file, source_root, destination_root, config):
        if file.source.suffix == ".srt":
            raise OSError("sidecar restore failed")
        return move(file, source_root, destination_root, config)

    monkeypatch.setattr(undo, "_move_file", fail_sidecar)
    # Hold the original inode open so a copied rollback cannot reuse it.
    with target.open("rb"):
        _, counts = undo.undo_transaction(original.data["transaction_id"], config)
    assert counts["ERROR"] == 1
    assert target.read_bytes() == b"movie" and not source.exists()
    refreshed = Transaction.load(config.state_dir / "transactions", original.data["transaction_id"])
    media_record = next(file for file in refreshed.data["items"][0]["files"] if file["destination"] == str(target))
    assert media_record["destination_state"]["inode"] == target.stat().st_ino
    monkeypatch.setattr(undo, "_move_file", move)
    _, counts = undo.undo_transaction(original.data["transaction_id"], config)
    assert counts["UNDONE"] == 1
    assert source.read_bytes() == b"movie" and source.with_suffix(".en.srt").read_bytes() == b"subtitle"


def test_undo_audit_restores_sidecar_without_moving_unchanged_media(tmp_path, monkeypatch, capsys):
    config_path, paths = setup(tmp_path, monkeypatch)
    source = paths["movie_lib"] / "We Live in Time (2024) [tmdbid-1100099]" / "We Live in Time (2024).mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"movie")
    sidecar = source.with_name("we.live.in.time.2024.en.srt")
    sidecar.write_bytes(b"subtitle")
    initial = source.stat()
    assert cli.main(["--config", str(config_path), "audit", "movies", "--auto"]) == 0
    output = capsys.readouterr().out
    transaction_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Transaction:"))
    assert source.exists() and not sidecar.exists()
    assert cli.main(["--config", str(config_path), "undo", transaction_id]) == 0
    capsys.readouterr()
    assert source.read_bytes() == b"movie" and sidecar.read_bytes() == b"subtitle"
    assert source.stat().st_ino == initial.st_ino and source.stat().st_mtime_ns == initial.st_mtime_ns
