import asyncio
import errno
import json
import os
from pathlib import Path

import pytest

from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.cli import review_source_current
from jellyorganize.identity.store import IdentityStore
from jellyorganize.models import Candidate
from jellyorganize.models import Proposal
from jellyorganize.naming.jellyfin import destinations
from jellyorganize.planning.ingest import plan_ingest
from jellyorganize.planning.store import PlanStore
from jellyorganize.scanner.incoming import scan_incoming


class NoSearchTMDb:
    async def search_movie(self, title, year=None):
        raise AssertionError("confirmed identity must not be searched again")


def touch(path: Path, content=b"media"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def confirmed_plan(config, tmp_path, source, candidate=None):
    candidate = candidate or Candidate(provider="tmdb", provider_id="438631", kind="movie", title="Dune", year=2021)
    identities = IdentityStore(tmp_path / "identities.sqlite3")
    item = next(item for item in scan_incoming(config, "movie").items if item.path == source)
    identities.confirm(item, candidate, "test")
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, NoSearchTMDb(), identities=identities))
    return PlanStore(tmp_path / "plans").create(proposals, config)


def test_manual_identity_saved_and_reused_without_network(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    assert plan.entries[0].status == "CONFIRMED"
    assert plan.entries[0].candidate.provider_id == "438631"
    loaded = PlanStore(tmp_path / "plans").load(plan.plan_id)
    assert loaded.entries[0].files[0].size == source.stat().st_size
    with pytest.raises(FileExistsError):
        (tmp_path / "plans" / f"{plan.plan_id}.json").open("x")


def test_pre_audit_saved_plan_still_loads_as_ingest(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    path = tmp_path / "plans" / f"{plan.plan_id}.json"
    payload = json.loads(path.read_text())
    del payload["workflow"]
    path.chmod(0o600)
    path.write_text(json.dumps(payload))
    assert PlanStore(tmp_path / "plans").load(plan.plan_id).workflow == "ingest"


def test_apply_moves_media_and_sidecar_without_overwrite(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.Release" / "Dune.2021.mkv"
    sidecar = source.with_name("Dune.2021.en.srt")
    touch(source, b"movie-data")
    touch(sidecar, b"subtitle")
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    transaction, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    target = plan.entries[0].destination
    assert target.read_bytes() == b"movie-data"
    assert (target.parent / "Dune (2021).en.srt").read_bytes() == b"subtitle"
    assert not source.exists() and not sidecar.exists()
    journal = json.loads(transaction.path.read_text())
    assert journal["plan_id"] == plan.plan_id
    assert journal["items"][0]["status"] == "applied"
    assert len(journal["items"][0]["files"]) == 2


def test_apply_accepts_symlinked_ancestor_of_configured_roots(tmp_path):
    actual_home = tmp_path / "home22" / "user"
    actual_home.mkdir(parents=True)
    home_link = tmp_path / "home"
    home_link.symlink_to(actual_home, target_is_directory=True)
    from jellyorganize.config import Config

    config = Config.model_validate({
        "movies": {"incoming": home_link / "media" / "Incoming" / "Movies",
                   "library": home_link / "media" / "Movies"},
        "tv": {"incoming": home_link / "media" / "Incoming" / "TV Shows",
               "library": home_link / "media" / "TV Shows"},
        "incoming": {"minimum_age_seconds": 0},
    })
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)

    _, counts = apply_plan(plan, config, tmp_path / "transactions")

    assert counts["APPLIED"] == 1
    assert not source.exists()
    assert plan.entries[0].destination.read_bytes() == b"media"


def test_stale_source_is_left_untouched(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    source.write_bytes(b"new data")
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["STALE"] == 1
    assert source.read_bytes() == b"new data"
    assert not plan.entries[0].destination.exists()


def test_existing_different_extension_is_conflict(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    touch(plan.entries[0].destination.with_suffix(".mp4"), b"existing")
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["CONFLICT"] == 1
    assert source.exists()
    assert plan.entries[0].destination.with_suffix(".mp4").read_bytes() == b"existing"


def test_symlinked_destination_parent_is_rejected(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    plan.entries[0].destination.parent.symlink_to(outside, target_is_directory=True)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["CONFLICT"] + counts["STALE"] == 1
    assert source.exists()
    assert not list(outside.iterdir())


def test_cross_filesystem_copy_is_verified(config, tmp_path, monkeypatch):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source, b"a" * 8192)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")

    monkeypatch.setattr("jellyorganize.filesystem.apply.os.link", exdev)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    assert plan.entries[0].destination.read_bytes() == b"a" * 8192
    assert not source.exists()


def test_cross_filesystem_verification_failure_preserves_source(config, tmp_path, monkeypatch):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source, b"original")
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")

    def bad_hash(descriptor, algorithm):
        return "different" if os.fstat(descriptor).st_dev == source.stat().st_dev and os.fstat(descriptor).st_ino == source.stat().st_ino else "hash"

    monkeypatch.setattr("jellyorganize.filesystem.apply.os.link", exdev)
    monkeypatch.setattr("jellyorganize.filesystem.apply._hash_fd", bad_hash)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["ERROR"] == 1
    assert source.read_bytes() == b"original"
    assert not plan.entries[0].destination.exists()


def test_failed_cross_filesystem_copy_preserves_source(config, tmp_path, monkeypatch):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source, b"original")
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")

    def failed_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("jellyorganize.filesystem.apply.os.link", exdev)
    monkeypatch.setattr("jellyorganize.filesystem.apply.os.write", failed_write)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["ERROR"] == 1
    assert source.read_bytes() == b"original"
    assert not plan.entries[0].destination.exists()


def test_failed_media_move_rolls_sidecar_back(config, tmp_path, monkeypatch):
    source = config.movies.incoming / "Dune.2021.Release" / "Dune.2021.mkv"
    sidecar = source.with_name("Dune.2021.en.srt")
    touch(source)
    touch(sidecar, b"subtitle")
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    original_link = os.link

    def fail_media(source_name, destination_name, **kwargs):
        if source_name.endswith(".mkv"):
            raise PermissionError("media move failed")
        return original_link(source_name, destination_name, **kwargs)

    monkeypatch.setattr("jellyorganize.filesystem.apply.os.link", fail_media)
    transaction, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["ERROR"] == 1
    assert source.exists() and sidecar.read_bytes() == b"subtitle"
    assert not plan.entries[0].destination.exists()
    assert not (plan.entries[0].destination.parent / "Dune (2021).en.srt").exists()
    assert json.loads(transaction.path.read_text())["items"][0]["files"][-1]["status"] == "rolled back"


def test_unsafe_plan_destination_is_rejected(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    outside = tmp_path / "outside.mkv"
    plan.entries[0].destination = outside
    plan.entries[0].files[0].destination = outside
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["STALE"] == 1
    assert source.exists() and not outside.exists()


def test_stale_item_does_not_block_another(config, tmp_path):
    first = config.movies.incoming / "Dune.2021.mkv"
    second = config.movies.incoming / "Arrival.2016.mkv"
    touch(first)
    touch(second)
    config.movies.library.mkdir(parents=True)
    candidates = {
        first: Candidate(provider="tmdb", provider_id="438631", kind="movie", title="Dune", year=2021),
        second: Candidate(provider="tmdb", provider_id="329865", kind="movie", title="Arrival", year=2016),
    }
    proposals = []
    for item in scan_incoming(config, "movie").items:
        target, sidecars = destinations(item, candidates[item.path], config)
        proposals.append(Proposal(item, "CONFIRMED", "manual identity", 1.0, candidates[item.path], target, sidecars))
    plan = PlanStore(tmp_path / "plans").create(proposals, config)
    first.write_bytes(b"changed")
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["STALE"] == 1 and counts["APPLIED"] == 1
    assert first.exists() and not second.exists()


def test_source_changed_during_staging_is_preserved_and_not_published(config, tmp_path, monkeypatch):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    original_link = os.link

    def change_source_after_link(source_name, destination_name, **kwargs):
        result = original_link(source_name, destination_name, **kwargs)
        source.write_bytes(b"changed while moving")
        return result

    monkeypatch.setattr("jellyorganize.filesystem.apply.os.link", change_source_after_link)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["ERROR"] == 1 or counts["STALE"] == 1
    assert source.exists()
    assert not plan.entries[0].destination.exists()
    assert list(plan.entries[0].destination.parent.glob(".jellyorganize-*.part"))


def test_review_refuses_changed_source(config, tmp_path):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    item = scan_incoming(config, "movie").items[0]
    assert review_source_current(plan.entries[0], item)
    source.write_bytes(b"replacement")
    assert not review_source_current(plan.entries[0], item)


def test_data_sync_failure_never_publishes_or_removes_source(config, tmp_path, monkeypatch):
    import stat
    source = config.movies.incoming / 'Dune.2021.mkv'
    touch(source, b'unsynchronized-media')
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, tmp_path, source)
    inode = source.stat().st_ino
    synchronize = os.fsync
    def fail_media_sync(descriptor):
        current = os.fstat(descriptor)
        if stat.S_ISREG(current.st_mode) and current.st_ino == inode:
            raise OSError(errno.EIO, 'media synchronization failed')
        return synchronize(descriptor)
    monkeypatch.setattr(os, 'fsync', fail_media_sync)
    _, counts = apply_plan(plan, config, tmp_path / 'transactions')
    assert counts['ERROR'] == 1
    assert source.read_bytes() == b'unsynchronized-media'
    assert not plan.entries[0].destination.exists()
