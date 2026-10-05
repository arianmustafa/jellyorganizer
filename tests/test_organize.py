import asyncio
import pytest

from jellyorganize import cli
from jellyorganize.metadata.base import ProviderError
from jellyorganize.models import Candidate, MediaItem
from jellyorganize.planning.ingest import plan_ingest
from jellyorganize.scanner.incoming import scan_incoming
from test_auto import FakeTMDb, setup
from test_planning import FakeTMDb as PlanningTMDb, FakeTVmaze, touch


def test_organize_defaults_to_incoming_only_and_can_undo(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch)
    source = paths["movie_in"] / "we.live.in.time.2024.mkv"
    sidecar = source.with_suffix(".en.srt")
    source.write_bytes(b"movie")
    sidecar.write_bytes(b"subtitle")
    existing = paths["tv_lib"] / "The Bear (2022)" / "The.Bear.S02E03.mkv"
    touch(existing)

    assert cli.main(["--config", str(config), "organize"]) == 0
    output = capsys.readouterr().out
    assert "APPLIED: 1" in output and "unique exact movie title and year" in output
    assert "Attention:" not in output
    transaction_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Transaction:"))
    target = paths["movie_lib"] / "We Live in Time (2024) [tmdbid-1100099]" / "We Live in Time (2024).mkv"
    assert target.read_bytes() == b"movie"
    assert target.with_suffix(".en.srt").read_bytes() == b"subtitle"
    assert not source.exists() and not sidecar.exists()
    assert existing.read_bytes() == b"unchanged"
    assert cli.main(["--config", str(config), "organize"]) == 0
    assert "Auto: no eligible items" in capsys.readouterr().out

    assert cli.main(["--config", str(config), "undo", transaction_id]) == 0
    assert "UNDONE: 1" in capsys.readouterr().out
    assert source.read_bytes() == b"movie" and sidecar.read_bytes() == b"subtitle"
    assert not target.exists()
    assert cli.main(["--config", str(config), "undo", transaction_id]) == 0
    assert "UNDONE: 0" in capsys.readouterr().out


def test_organize_dry_run_and_higher_threshold_do_not_move(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch, threshold=0.99)
    source = paths["movie_in"] / "we.live.in.time.2024.mkv"
    source.write_bytes(b"movie")
    assert cli.main(["--config", str(config), "organize", "movies"]) == 2
    assert source.exists()
    capsys.readouterr()
    config.write_text(config.read_text().replace("0.99", "0.97"))
    assert cli.main(["--config", str(config), "organize", "movies", "--dry-run"]) == 0
    assert "[CONFIRMED]" in capsys.readouterr().out
    assert source.exists() and not list(paths["movie_lib"].rglob("*.mkv"))
    assert not list((tmp_path / "state" / "jellyorganize" / "transactions").glob("*.json"))


@pytest.mark.parametrize("failure", ["ambiguous", "wrong_details", "wrong_id", "provider_failure", "yearless", "conflicting_folder"])
def test_movie_auto_leaves_uncertain_files_while_applying_safe_tv(tmp_path, monkeypatch, capsys, failure):
    class UncertainMovie(FakeTMDb):
        async def search_movie(self, title, year=None):
            rows = await super().search_movie(title, year)
            if failure == "ambiguous":
                rows.append(rows[0].model_copy(update={"provider_id": "99"}))
            return rows

        async def get_movie(self, movie_id):
            if failure == "provider_failure":
                raise ProviderError("movie details unavailable")
            row = await super().get_movie(movie_id)
            if failure == "wrong_details":
                row.year = 2023
            if failure == "wrong_id":
                row.provider_id = "99"
            return row

    config, paths = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "TMDbProvider", UncertainMovie)
    source = paths["movie_in"] / ("we.live.in.time.mkv" if failure == "yearless" else "we.live.in.time.2024.mkv")
    if failure == "conflicting_folder":
        source = paths["movie_in"] / "A Different Movie (2024)" / source.name
    touch(source)
    safe_tv = paths["tv_in"] / "The Bear (2022)" / "The.Bear.S02E03.mkv"
    touch(safe_tv)
    assert cli.main(["--config", str(config), "organize"]) == (1 if failure == "provider_failure" else 2)
    output = capsys.readouterr().out
    assert "APPLIED: 1" in output
    assert source.exists() and not safe_tv.exists()


def test_movie_unicode_titles_are_distinct(config):
    class UnicodeMovies(PlanningTMDb):
        async def search_movie(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="1", kind="movie", title="千与千寻", year=2001)]

    item = MediaItem(path=config.movies.incoming / "film.mkv", root=config.movies.incoming,
                     kind="movie", hints={"title": "天空之城", "year": 2001})
    from jellyorganize.models import ScanResult
    proposal = asyncio.run(plan_ingest(ScanResult(items=[item]), config, UnicodeMovies()))[0]
    assert proposal.status == "REVIEW" and proposal.candidate is None


class ShiftedTMDb(PlanningTMDb):
    async def search_series(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="8592", kind="tv", title="Parks and Recreation", year=2009)]

    async def get_series(self, series_id):
        return (await self.search_series(""))[0]

    async def get_series_episode(self, series_id, season, episode):
        return {3: "Doppelgängers", 4: "Gin It Up!"}.get(episode)

    async def get_series_external_ids(self, series_id):
        return "tt1266020", 84912

    async def get_series_season_episodes(self, series_id, season):
        return {3: "Doppelgängers", 4: "Gin It Up!"}


class ShiftedTVmaze(FakeTVmaze):
    async def search_series(self, title, year=None):
        return [Candidate(provider="tvmaze", provider_id="174", kind="tv", title="Parks and Recreation", year=2009,
                          imdb_id="tt1266020", tvdb_id=84912)]

    async def get_series_episodes(self, series_id):
        return [(6, 4, "Doppelgängers"), (6, 5, "Gin It Up!")]


@pytest.mark.parametrize("explicit", [False, True])
def test_tv_release_number_maps_to_unique_catalog_title_and_applies(config, tmp_path, explicit):
    from jellyorganize.filesystem.apply import apply_plan
    from jellyorganize.planning.store import PlanStore

    folder = "Parks and Recreation (2009)" + (" [tmdbid-8592]" if explicit else "")
    source = config.tv.incoming / folder / "Season 6" / "Parks and Recreation - S06E04 - Doppelgängers.mkv"
    touch(source)
    subtitle = source.with_suffix(".en.srt")
    touch(subtitle)
    config.tv.library.mkdir(parents=True)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, ShiftedTMDb(), ShiftedTVmaze()))[0]
    assert proposal.status == "CONFIRMED" and proposal.confidence == 0.99
    assert proposal.destination.name == "Parks and Recreation - S06E03 - Doppelgängers.mkv"
    plan = PlanStore(tmp_path / "plans").create([proposal], config)
    _, counts = apply_plan(plan, config, tmp_path / "transactions", auto_threshold=0.97)
    assert counts["APPLIED"] == 1
    assert not source.exists() and proposal.destination.exists()
    assert proposal.destination.with_suffix(".en.srt").exists()


@pytest.mark.parametrize("failure", ["ids", "ambiguous_tmdb", "ambiguous_tvmaze", "wrong_number", "no_title", "occupied"])
def test_tv_number_mapping_requires_all_evidence(config, failure):
    class TMDb(ShiftedTMDb):
        async def get_series_season_episodes(self, series_id, season):
            return {3: "Doppelgängers", 7: "Doppelgängers"} if failure == "ambiguous_tmdb" else await super().get_series_season_episodes(series_id, season)

    class TVmaze(ShiftedTVmaze):
        async def search_series(self, title, year=None):
            rows = await super().search_series(title, year)
            if failure == "ids":
                rows[0].imdb_id = "tt99999"
            return rows

        async def get_series_episodes(self, series_id):
            return ([(6, 4, "Doppelgängers"), (6, 9, "Doppelgängers")] if failure == "ambiguous_tvmaze" else
                    [(6, 9, "Doppelgängers")] if failure == "wrong_number" else await super().get_series_episodes(series_id))

    name = "Parks and Recreation - S06E04" + ("" if failure == "no_title" else " - Doppelgängers") + ".mkv"
    source = config.tv.incoming / "Parks and Recreation (2009)" / name
    touch(source)
    if failure == "occupied":
        touch(config.tv.library / "Parks and Recreation (2009) [tmdbid-8592]" / "Season 06" /
              "Parks and Recreation - S06E03 - Doppelgängers.mkv")
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, TMDb(), TVmaze()))[0]
    assert proposal.status == ("CONFLICT" if failure == "occupied" else "REVIEW")
    assert source.exists()


def test_missing_incoming_is_an_automatic_error(tmp_path, monkeypatch, capsys):
    config, paths = setup(tmp_path, monkeypatch)
    paths["movie_in"].rmdir()
    assert cli.main(["--config", str(config), "organize", "movies"]) == 1
    assert "Incoming root is missing" in capsys.readouterr().out
