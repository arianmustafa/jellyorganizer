import json
from pathlib import Path

from jellyorganize import cli
from jellyorganize.models import Candidate
from jellyorganize.planning.store import PlanStore
from jellyorganize.config import load_config


class FakeTMDb:
    def __init__(self, token, cache, client):
        pass

    async def search_movie(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="1100099", kind="movie", title="We Live in Time", year=2024)]

    async def get_movie(self, movie_id):
        return Candidate(provider="tmdb", provider_id=movie_id, kind="movie", title="We Live in Time", year=2024)

    async def search_series(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="136315", kind="tv", title="The Bear", year=2022)]

    async def get_series(self, series_id):
        return Candidate(provider="tmdb", provider_id=series_id, kind="tv", title="The Bear", year=2022)

    async def get_series_external_ids(self, series_id):
        return None, None

    async def get_series_episode(self, series_id, season, episode):
        return {1: "Pilot", 3: "Sundae"}.get(episode)


class FakeTVmaze:
    def __init__(self, cache, client):
        pass

    async def search_series(self, title, year=None):
        return [Candidate(provider="tvmaze", provider_id="20", kind="tv", title="The Bear", year=2022)]

    async def get_series_episode(self, series_id, season, episode):
        return {1: "Pilot", 3: "Sundae"}.get(episode)


def setup(tmp_path, monkeypatch, *, threshold=None):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "TMDbProvider", FakeTMDb)
    monkeypatch.setattr(cli, "TVmazeProvider", FakeTVmaze)
    paths = {
        "movie_in": tmp_path / "Incoming" / "Movies",
        "movie_lib": tmp_path / "Movies",
        "tv_in": tmp_path / "Incoming" / "TV Shows",
        "tv_lib": tmp_path / "TV Shows",
    }
    for path in paths.values():
        path.mkdir(parents=True)
    config = tmp_path / "config.toml"
    matching = f"[matching]\nauto_apply_threshold = {threshold}\n" if threshold is not None else ""
    config.write_text(f'[movies]\nincoming = "{paths["movie_in"]}"\nlibrary = "{paths["movie_lib"]}"\n'
                      f'[tv]\nincoming = "{paths["tv_in"]}"\nlibrary = "{paths["tv_lib"]}"\n'
                      f'[incoming]\nminimum_age_seconds = 0\nstability_seconds = 0\n{matching}')
    return config, paths


def plan_id(output):
    return next(line.split()[1] for line in output.splitlines() if line.startswith("Plan:"))


def test_auto_audit_applies_only_strong_tv_and_is_idempotent(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch)
    strong = paths["tv_lib"] / "The Bear (2022)" / "Season 2" / "The.Bear.S02E03.mkv"
    weak = paths["tv_lib"] / "The.Bear.S01E01.mkv"
    strong.parent.mkdir(parents=True)
    strong.write_bytes(b"strong")
    weak.write_bytes(b"weak")

    assert cli.main(["--config", str(config), "audit", "tv", "--auto"]) == 2
    output = capsys.readouterr().out
    assert "[CONFIRMED]" in output and "[REVIEW]" in output
    assert "APPLIED: 1" in output and "UNTOUCHED: 1" in output
    plan = PlanStore(load_config(config).state_dir / "plans").load(plan_id(output))
    assert plan.workflow == "audit"
    assert {entry.status for entry in plan.entries} == {"CONFIRMED", "REVIEW"}
    target = paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 02" / "The Bear - S02E03 - Sundae.mkv"
    assert target.read_bytes() == b"strong"
    assert not strong.exists() and weak.read_bytes() == b"weak"
    journals = list((load_config(config).state_dir / "transactions").glob("*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text())["auto_threshold"] == 0.97

    assert cli.main(["--config", str(config), "audit", "tv", "--auto"]) == 2
    repeated = capsys.readouterr().out
    assert "Auto: no eligible items" in repeated
    assert len(list((load_config(config).state_dir / "transactions").glob("*.json"))) == 1


def test_auto_does_not_promote_movie_single_provider_match(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch, threshold=0.90)
    with config.open("a") as stream:
        stream.write("confirm_exact_movies = false\n")
    source = paths["movie_in"] / "we.live.in.time.2024.mkv"
    source.write_bytes(b"movie")

    assert cli.main(["--config", str(config), "ingest", "movies", "--auto"]) == 2
    output = capsys.readouterr().out
    assert "below automatic safety threshold" in output
    assert "Auto: no eligible items" in output
    assert source.read_bytes() == b"movie"
    assert not list(paths["movie_lib"].rglob("*.mkv"))


def test_auto_audit_explicit_id_applies_without_tvmaze(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch)
    source = paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 2" / "The.Bear.S02E03.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"episode")

    assert cli.main(["--config", str(config), "audit", "tv", "--auto"]) == 0
    output = capsys.readouterr().out
    assert "APPLIED: 1" in output and "Confidence: 1.00" in output
    target = paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 02" / "The Bear - S02E03 - Sundae.mkv"
    assert target.read_bytes() == b"episode"


def test_auto_ingest_tv_applies_only_nonconflicting_episodes(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch)
    package = paths["tv_in"] / "The Bear (2022)"
    first = package / "The.Bear.S01E01.mkv"
    third = package / "The.Bear.S02E03.mkv"
    package.mkdir()
    first.write_bytes(b"incoming duplicate")
    third.write_bytes(b"new episode")
    existing = paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 01" / "The Bear - S01E01 - Pilot.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"library release")

    assert cli.main(["--config", str(config), "ingest", "tv", "--auto"]) == 3
    output = capsys.readouterr().out
    assert "[CONFLICT]" in output and "APPLIED: 1" in output
    target = paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 02" / "The Bear - S02E03 - Sundae.mkv"
    assert target.read_bytes() == b"new episode"
    assert existing.read_bytes() == b"library release"
    assert first.read_bytes() == b"incoming duplicate"
    assert not third.exists()


def test_auto_rechecks_saved_source_before_move(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch)
    source = paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 2" / "The.Bear.S02E03.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"episode")
    original_apply = cli.apply_plan

    def change_after_plan(plan, config, transaction_root, *, auto_threshold=None):
        source.write_bytes(b"modified after planning")
        return original_apply(plan, config, transaction_root, auto_threshold=auto_threshold)

    monkeypatch.setattr(cli, "apply_plan", change_after_plan)
    assert cli.main(["--config", str(config), "audit", "tv", "--auto"]) == 1
    output = capsys.readouterr().out
    assert "STALE: 1" in output
    assert source.read_bytes() == b"modified after planning"
    assert not (paths["tv_lib"] / "The Bear (2022) [tmdbid-136315]" / "Season 02").exists()


def test_auto_audit_yearless_series_with_matching_external_ids(tmp_path, monkeypatch, capsys):
    class CommunityTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="18347", kind="tv", title="Community", year=2009)]

        async def get_series_episode(self, series_id, season, episode):
            return "Pilot"

        async def get_series_external_ids(self, series_id):
            return "tt1439629", 94571

    class CommunityTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="318", kind="tv", title="Community", year=2009,
                              imdb_id="tt1439629", tvdb_id=94571)]

        async def get_series_episode(self, series_id, season, episode):
            return "Pilot"

    config, paths = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "TMDbProvider", CommunityTMDb)
    monkeypatch.setattr(cli, "TVmazeProvider", CommunityTVmaze)
    source = paths["tv_lib"] / "Community" / "S01" / "Community.S01E01.REPACK.1080p.Bluray.x265-HiQVE.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"episode")

    assert cli.main(["--config", str(config), "audit", "tv", "--auto"]) == 0
    output = capsys.readouterr().out
    assert "TMDb and TVmaze external IDs agree" in output
    assert "APPLIED: 1" in output
    target = (paths["tv_lib"] / "Community (2009) [tmdbid-18347]" / "Season 01" /
              "Community - S01E01 - Pilot.mkv")
    assert target.read_bytes() == b"episode"
    assert not source.exists()
