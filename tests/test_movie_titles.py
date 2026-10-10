import asyncio

import pytest

from jellyorganize.models import Candidate
from jellyorganize.planning.ingest import plan_ingest
from jellyorganize.scanner.incoming import scan_incoming


FILENAME = "Spider-Man.Brand.New.Day.2026.2160p.iT.WEB-DL.DV.HDR10+[Ben The Men].mp4"
PACKAGE = "Spider-Man.Brand.New.Day.2026.2160p.iT.WEB-DL.DV.HDR10+.DDP5.1.Atmos.H265.MP4-BTM"


class Movies:
    def __init__(self, *, ambiguous=False, details_year=2026):
        self.queries = []
        self.ambiguous = ambiguous
        self.details_year = details_year

    async def search_movie(self, title, year=None):
        self.queries.append((title, year))
        row = Candidate(provider="tmdb", provider_id="969681", kind="movie",
                        title="Spider-Man: Brand New Day", year=2026)
        return [row, row.model_copy(update={"provider_id": "99"})] if self.ambiguous else [row]

    async def get_movie(self, movie_id):
        return Candidate(provider="tmdb", provider_id=movie_id, kind="movie",
                         title="Spider-Man: Brand New Day", year=self.details_year)


def proposal(config, folder, provider):
    source = config.movies.incoming / folder / FILENAME
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"movie")
    result = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, provider))[0]
    assert source.read_bytes() == b"movie"
    return result


@pytest.mark.parametrize("folder", [PACKAGE, "Spider-Man Brand New Day (2026)"])
def test_folder_corroborates_hyphenated_title(config, folder):
    provider = Movies()
    result = proposal(config, folder, provider)
    assert result.status == "CONFIRMED"
    assert result.candidate.provider_id == "969681"
    assert result.destination.name == "Spider-Man Brand New Day (2026).mp4"
    assert provider.queries == [("Spider-Man Brand New Day", 2026)]


@pytest.mark.parametrize("folder", ["", "unrelated", "Brand New Day (2026)",
                                   PACKAGE.replace("2026", "2025"), "Spider-Man Brand New Day"])
def test_unverified_folder_cannot_restore_title(config, folder):
    assert proposal(config, folder, Movies()).status == "REVIEW"


@pytest.mark.parametrize("provider", [Movies(ambiguous=True), Movies(details_year=2025)])
def test_corrected_title_still_requires_unique_verified_match(config, provider):
    assert proposal(config, PACKAGE, provider).status == "REVIEW"


def test_actual_leading_release_group_is_preserved(config):
    source = config.movies.incoming / "Dune.2021.1080p.WEB-DL.x264-BTM" / "FoV-Dune.2021.1080p.WEB-DL.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"movie")
    item = scan_incoming(config, "movie").items[0]
    from jellyorganize.metadata.matcher import local_title_year
    assert local_title_year(item) == ("Dune", 2021)
