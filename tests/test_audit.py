import asyncio
import json
from pathlib import Path

from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.identity.store import IdentityStore
from jellyorganize.models import Candidate
from jellyorganize.planning.audit import plan_audit
from jellyorganize.planning.store import PlanStore
from jellyorganize.scanner.library import scan_library


class FakeTMDb:
    async def search_movie(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="329865", kind="movie", title="Arrival", year=2016)]

    async def get_movie(self, movie_id):
        return Candidate(provider="tmdb", provider_id=movie_id, kind="movie", title="Arrival", year=2016)

    async def get_series(self, series_id):
        return Candidate(provider="tmdb", provider_id=series_id, kind="tv", title="The Bear", year=2022)

    async def get_series_episode(self, series_id, season, episode):
        return {1: "Pilot", 2: "Second"}.get(episode)


class NoNetworkTMDb:
    def __getattr__(self, name):
        raise AssertionError(f"canonical item must not use metadata: {name}")


def touch(path: Path, content=b"media"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def make_plan(config, tmp_path, kind, provider=None):
    proposals = asyncio.run(plan_audit(scan_library(config, kind), config, provider or FakeTMDb(),
                                       identities=IdentityStore(tmp_path / "identities.sqlite3")))
    return PlanStore(tmp_path / "plans").create(proposals, config, (kind,), workflow="audit")


def test_canonical_library_media_skipped_without_network(config, tmp_path):
    movie = config.movies.library / "Arrival (2016) [tmdbid-329865]" / "Arrival (2016).mkv"
    episode = config.tv.library / "The Bear (2022) [tmdbid-136315]" / "Season 01" / "The Bear - S01E01 - Pilot.mkv"
    touch(movie)
    touch(episode)
    movie_plan = make_plan(config, tmp_path, "movie", NoNetworkTMDb())
    tv_plan = make_plan(config, tmp_path, "tv", NoNetworkTMDb())
    assert movie_plan.entries[0].status == "SKIP"
    assert tv_plan.entries[0].status == "SKIP"
    assert movie.exists() and episode.exists()


def test_audit_repairs_movie_and_sidecar_in_library(config, tmp_path):
    source = config.movies.library / "Arrival (2016) [tmdbid-329865]" / "Arrival.2016.1080p.mkv"
    subtitle = source.with_name("Arrival.2016.1080p.en.srt")
    touch(source)
    touch(subtitle, b"subtitle")
    plan = make_plan(config, tmp_path, "movie")
    entry = plan.entries[0]
    assert entry.status == "CONFIRMED"
    assert entry.source_root == entry.destination_root == config.movies.library
    assert source.exists() and subtitle.exists()  # audit itself is read-only

    transaction, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    assert entry.destination.read_bytes() == b"media"
    assert (entry.destination.parent / "Arrival (2016).en.srt").read_bytes() == b"subtitle"
    assert not source.exists() and not subtitle.exists()
    assert json.loads(transaction.path.read_text())["items"][0]["operation"] == "audit"


def test_audit_moves_movie_out_of_malformed_folder(config, tmp_path):
    source = config.movies.library / "Arrival (2016) [tmdbid-329865]" / "subfolder" / "Arrival.2016.mkv"
    touch(source)
    plan = make_plan(config, tmp_path, "movie")
    assert plan.entries[0].status == "CONFIRMED"
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    assert plan.entries[0].destination.exists() and not source.exists()
    assert source.parent.exists()  # no automatic directory cleanup


def test_audit_series_with_separate_seasons_and_loose_episodes(config, tmp_path):
    series = config.tv.library / "The Bear (2022) [tmdbid-136315]"
    first = series / "Season 1" / "The.Bear.S01E01.mkv"
    second = series / "The.Bear.S02E02.mkv"
    touch(first)
    touch(second)
    plan = make_plan(config, tmp_path, "tv")
    assert len(plan.entries) == 2
    assert all(entry.status == "CONFIRMED" for entry in plan.entries)
    assert {entry.destination.parent.name for entry in plan.entries} == {"Season 01", "Season 02"}
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 2
    assert not first.exists() and not second.exists()
    assert all(entry.destination.exists() for entry in plan.entries)


def test_audit_same_media_path_can_repair_only_sidecar(config, tmp_path):
    source = config.movies.library / "Arrival (2016) [tmdbid-329865]" / "Arrival (2016).mkv"
    subtitle = source.with_name("Arrival.2016.en.srt")
    touch(source)
    touch(subtitle, b"subtitle")
    plan = make_plan(config, tmp_path, "movie")
    assert plan.entries[0].status == "CONFIRMED"
    assert plan.entries[0].files[0].source == plan.entries[0].files[0].destination
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    assert source.exists() and not subtitle.exists()
    assert (source.parent / "Arrival (2016).en.srt").exists()


def test_audit_missing_provider_id_requires_review(config, tmp_path):
    source = config.movies.library / "Arrival (2016)" / "Arrival (2016).mkv"
    touch(source)
    plan = make_plan(config, tmp_path, "movie")
    assert plan.entries[0].status == "REVIEW"
    assert "tmdbid-329865" in str(plan.entries[0].destination)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["UNTOUCHED"] == 1
    assert source.exists()


def test_audit_existing_destination_is_conflict(config, tmp_path):
    source = config.movies.library / "Arrival (2016) [tmdbid-329865]" / "Arrival.2016.mkv"
    target = source.with_name("Arrival (2016).mkv")
    touch(source)
    touch(target, b"existing")
    plan = make_plan(config, tmp_path, "movie")
    assert any(entry.status == "CONFLICT" for entry in plan.entries)
    assert source.exists() and target.read_bytes() == b"existing"


def test_audit_does_not_skip_two_canonical_movie_releases(config, tmp_path):
    folder = config.movies.library / "Arrival (2016) [tmdbid-329865]"
    touch(folder / "Arrival (2016).mkv")
    touch(folder / "Arrival (2016).mp4")
    plan = make_plan(config, tmp_path, "movie")
    assert len(plan.entries) == 2
    assert {entry.status for entry in plan.entries} == {"CONFLICT"}


def test_audit_stale_plan_does_not_move_media(config, tmp_path):
    source = config.movies.library / "Arrival (2016) [tmdbid-329865]" / "Arrival.2016.mkv"
    touch(source)
    plan = make_plan(config, tmp_path, "movie")
    source.write_bytes(b"changed")
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["STALE"] == 1
    assert source.read_bytes() == b"changed"
    assert not plan.entries[0].destination.exists()


def test_audit_ignores_featurettes_and_matches_numbered_special(config):
    class DoctorTMDb(FakeTMDb):
        async def get_series(self, series_id):
            return Candidate(provider="tmdb", provider_id=series_id, kind="tv", title="Doctor Who", year=2005)

        async def get_series_episode(self, series_id, season, episode):
            return "A Numbered Special" if (season, episode) == (0, 1) else None

    series = config.tv.library / "Doctor Who (2005) [tmdbid-57243]"
    featurette = series / "Season 01" / "Featurettes" / "Featurette - Interview.mkv"
    special = series / "Season 00" / "Doctor.Who.S00E01.mkv"
    touch(featurette)
    touch(special)
    scan = scan_library(config, "tv")
    proposals = asyncio.run(plan_audit(scan, config, DoctorTMDb()))

    assert len(proposals) == 2
    assert next(p for p in proposals if p.item.path == featurette).status == "SKIP"
    special_plan = next(p for p in proposals if p.item.path == special)
    assert special_plan.status == "CONFIRMED"
    assert special_plan.destination.name == "Doctor Who - S00E01 - A Numbered Special.mkv"
    assert featurette.exists() and special.exists()


def test_season_episode_zero_is_not_guessed_as_a_special(config):
    class DoctorTMDb(FakeTMDb):
        async def get_series(self, series_id):
            return Candidate(provider="tmdb", provider_id=series_id, kind="tv", title="Doctor Who", year=2005)

        async def get_series_episode(self, series_id, season, episode):
            return "The Christmas Invasion" if (season, episode) == (0, 2) else None

    folder = config.tv.library / "Doctor Who (2005) [tmdbid-57243]" / "S02"
    unknown = folder / "Doctor.Who.S02E00.mkv"
    touch(unknown)
    proposal = asyncio.run(plan_audit(scan_library(config, "tv"), config, DoctorTMDb()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "TMDb episode missing"

    verified = folder / "Doctor.Who.S00E02.mkv"
    unknown.rename(verified)
    proposal = asyncio.run(plan_audit(scan_library(config, "tv"), config, DoctorTMDb()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.destination.parent.name == "Season 00"
