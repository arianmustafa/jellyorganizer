import asyncio
import errno
import json
import os

import pytest

from jellyorganize import cli
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.filesystem import doctor
from jellyorganize.filesystem.undo import undo_transaction
from jellyorganize.models import Candidate, MediaItem, Proposal
from jellyorganize.metadata.explanation import explain
from jellyorganize.naming.movie_versions import label_for
from jellyorganize.planning.audit import plan_audit
from jellyorganize.planning.ingest import plan_ingest
from jellyorganize.planning.store import PlanStore
from jellyorganize.scanner.incoming import scan_incoming
from jellyorganize.scanner.library import scan_library
from test_planning import FakeTMDb, touch


def movie_plan(config, tmp_path):
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, FakeTMDb()))
    return PlanStore(tmp_path / "plans").create(proposals, config)


def test_two_movie_versions_apply_with_sidecars_then_undo(config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config.naming.movie_versions = True
    config.filesystem.mode = "hardlink"
    config.movies.library.mkdir()
    sources = []
    for resolution in ("1080p", "2160p"):
        source = config.movies.incoming / resolution / f"Dune.2021.{resolution}.mkv"
        touch(source)
        touch(source.with_suffix(".en.srt"))
        sources.append(source)
    plan = movie_plan(config, tmp_path)
    PlanStore(config.state_dir / "plans").save(plan)
    assert all(entry.status == "CONFIRMED" for entry in plan.entries)
    assert {entry.destination.name for entry in plan.entries} == {
        "Dune (2021) [tmdbid-438631] - 1080p.mkv", "Dune (2021) [tmdbid-438631] - 2160p.mkv"}
    # Plans keep their filenames even if the config changes before apply.
    config.naming.movie_versions = False
    transaction, counts = apply_plan(plan, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 2
    for entry in plan.entries:
        assert os.path.samefile(entry.source, entry.destination)
        assert os.path.samefile(entry.files[1].source, entry.files[1].destination)
    config.naming.movie_versions = True
    audit = asyncio.run(plan_audit(scan_library(config, "movie"), config, FakeTMDb()))
    assert all(row.status == "SKIP" for row in audit)
    _, counts = undo_transaction(transaction.data["transaction_id"], config)
    assert counts["UNDONE"] == 2
    assert all(source.exists() for source in sources)
    assert not list(config.movies.library.rglob("*.mkv"))


@pytest.mark.parametrize("versions", [False, True])
def test_same_movie_version_across_extensions_conflicts(config, tmp_path, versions):
    config.naming.movie_versions = versions
    for extension in ("mkv", "mp4"):
        touch(config.movies.incoming / extension / f"Dune.2021.1080p.{extension}")
    plan = movie_plan(config, tmp_path)
    assert {entry.status for entry in plan.entries} == {"CONFLICT"}


def test_existing_version_keeps_bytes_when_another_is_added(config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config.naming.movie_versions = True
    config.movies.library.mkdir()
    first = config.movies.incoming / "Dune.2021.1080p.mkv"
    touch(first)
    initial = movie_plan(config, tmp_path)
    PlanStore(config.state_dir / "plans").save(initial)
    apply_plan(initial, config, config.state_dir / "transactions")
    existing = initial.entries[0].destination
    identity = existing.stat().st_ino
    touch(config.movies.incoming / "Dune.2021.2160p.mkv")
    second = movie_plan(config, tmp_path)
    PlanStore(config.state_dir / "plans").save(second)
    assert second.entries[0].status == "CONFIRMED"
    transaction, counts = apply_plan(second, config, config.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    undo_transaction(transaction.data["transaction_id"], config)
    assert existing.stat().st_ino == identity and existing.read_bytes() == b"unchanged"


def test_explicit_movie_label_and_edition(config, tmp_path):
    config.naming.movie_versions = True
    touch(config.movies.incoming / "Dune.2021.2160p.[version-Theatrical].mkv")
    touch(config.movies.incoming / "Dune.2021.2160p.Extended.mkv")
    plan = movie_plan(config, tmp_path)
    assert all(entry.status == "CONFIRMED" for entry in plan.entries)
    assert {entry.destination.name for entry in plan.entries} == {
        "Dune (2021) [tmdbid-438631] - Theatrical.mkv",
        "Dune (2021) [tmdbid-438631] - Extended 2160p.mkv"}


@pytest.mark.parametrize("label", ["", "../escape", 'Bad"Label', "x" * 81])
def test_unsafe_movie_label_rejected(tmp_path, label):
    item = MediaItem(tmp_path / "Dune.mkv", "movie", tmp_path, hints={"version_label": label})
    with pytest.raises(ValueError):
        label_for(item)


def test_legacy_movie_blocks_version_until_audited(config, tmp_path):
    config.naming.movie_versions = True
    touch(config.movies.library / "Dune (2021) [tmdbid-438631]" / "Dune (2021).mkv")
    touch(config.movies.incoming / "Dune.2021.2160p.mkv")
    assert movie_plan(config, tmp_path).entries[0].status == "CONFLICT"
    repairs = asyncio.run(plan_audit(scan_library(config, "movie"), config, FakeTMDb()))
    assert repairs[0].status == "CONFIRMED"
    assert repairs[0].destination.name.endswith(" - Original.mkv")


def test_matching_evidence_roundtrips_and_explain_is_offline(config, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    touch(config.movies.incoming / "Dune.2000.mkv")
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, FakeTMDb()))
    store = PlanStore(config.state_dir / "plans")
    plan = store.create(proposals, config)
    before = (store.root / f"{plan.plan_id}.json").read_bytes()
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "TMDbProvider", lambda *args: pytest.fail("explain made a provider request"))
    assert cli.main(["explain", plan.plan_id, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    evidence = output[0]["matching"]
    assert evidence["effective_year"] == 2000
    assert evidence["candidates"][0]["title_matches"]
    assert not evidence["candidates"][0]["year_matches"]
    assert (store.root / f"{plan.plan_id}.json").read_bytes() == before
    assert cli.main(["explain", plan.plan_id]) == 0
    assert "year differs" in capsys.readouterr().out


def test_matching_evidence_records_corrected_hyphen_title(tmp_path):
    path = tmp_path / "Spider-Man.Brand.New.Day.2026.mkv"
    item = MediaItem(path, "movie", tmp_path, hints={"title": "Man Brand New Day", "year": 2026,
        "release_group": "Spider", "package_title": "Spider Man Brand New Day", "package_year": 2026})
    candidate = Candidate(provider="tmdb", provider_id="969681", kind="movie",
                          title="Spider-Man: Brand New Day", year=2026)
    evidence = explain(Proposal(item, "CONFIRMED", "exact", candidate=candidate))
    assert evidence["parsed_title"] == "Man Brand New Day"
    assert evidence["effective_title"] == "Spider Man Brand New Day"
    assert evidence["candidates"][0]["title_matches"]


def test_doctor_real_links_cleanup_and_preserve_media(tmp_path):
    source, destination = tmp_path / "Incoming", tmp_path / "Movies"
    source.mkdir()
    destination.mkdir()
    media = source / "movie.mkv"
    media.write_bytes(b"untouched")
    before = media.stat()
    result = doctor.probe(source, destination, "hardlink")
    assert result["status"] == "OK" and result["link_supported"]
    assert list(source.iterdir()) == [media] and list(destination.iterdir()) == []
    assert media.stat() == before


@pytest.mark.parametrize("mode,status", [("move", "OK"), ("hardlink", "ERROR")])
def test_doctor_mount_or_permission_error(tmp_path, monkeypatch, mode, status):
    source, destination = tmp_path / "Incoming", tmp_path / "Movies"
    source.mkdir()
    destination.mkdir()
    def fail(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(doctor.os, "link", fail)
    assert doctor.probe(source, destination, mode)["status"] == status
    assert not list(source.iterdir()) and not list(destination.iterdir())


def test_doctor_does_not_create_missing_roots_or_follow_symlink(tmp_path):
    source, destination = tmp_path / "Incoming", tmp_path / "Movies"
    source.mkdir()
    assert doctor.probe(source, destination, "move")["status"] == "ERROR"
    assert not destination.exists()
    destination.symlink_to(source, target_is_directory=True)
    assert doctor.probe(source, destination, "hardlink")["status"] == "ERROR"


def test_doctor_cli_json(config, monkeypatch, capsys):
    for root in (config.movies.incoming, config.tv.incoming, config.movies.library, config.tv.library):
        root.mkdir(parents=True)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]
