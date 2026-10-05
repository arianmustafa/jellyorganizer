import asyncio

import httpx
import pytest

from jellyorganize.metadata.cache import MetadataCache
from jellyorganize.metadata.base import get_json, ProviderError
from jellyorganize.metadata.tmdb import TMDbProvider
from jellyorganize.metadata.tvmaze import TVmazeProvider


def test_tmdb_cache_and_tvmaze_search(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.host == "api.themoviedb.org":
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(200, json={"total_pages": 1, "results": [{"id": 438631, "title": "Dune", "release_date": "2021-09-15"}]})
        return httpx.Response(200, json=[{"show": {"id": 1, "name": "The Bear", "premiered": "2022-06-23", "externals": {"imdb": "tt14452776"}}}])

    async def run():
        cache = MetadataCache(tmp_path / "metadata.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tmdb = TMDbProvider("test-token", cache, client)
            maze = TVmazeProvider(cache, client)
            assert (await tmdb.search_movie("Dune", 2021))[0].provider_id == "438631"
            assert (await tmdb.search_movie("Dune", 2021))[0].year == 2021
            assert (await maze.search_series("The Bear"))[0].imdb_id == "tt14452776"
            assert len(requests) == 2
        assert cache.stats() == (2, 2)
        assert cache.clear() == 2
    asyncio.run(run())


def test_rate_limit_retries_then_succeeds(monkeypatch):
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(429 if attempts == 1 else 200, json={} if attempts == 1 else {"ok": True})

    async def no_wait(delay):
        pass

    monkeypatch.setattr("jellyorganize.metadata.base.asyncio.sleep", no_wait)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await get_json(client, "https://api.tvmaze.com/test") == {"ok": True}
    asyncio.run(run())
    assert attempts == 2


def test_malformed_tmdb_id_cannot_become_a_destination(tmp_path):
    def respond(request):
        return httpx.Response(200, json={"total_pages": 1, "results": [{"id": "../../outside", "title": "Dune", "release_date": "2021-09-15"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tmdb = TMDbProvider("token", MetadataCache(tmp_path / "cache.sqlite3"), client)
            assert await tmdb.search_movie("Dune", 2021) == []
    asyncio.run(run())


def test_movie_search_reads_every_page_and_caches_them(tmp_path):
    requests = []

    def respond(request):
        page = int(request.url.params.get("page", "1"))
        requests.append(page)
        return httpx.Response(200, json={"total_pages": 2, "results": [
            {"id": page, "title": "Dune", "release_date": "2021-09-15"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tmdb = TMDbProvider("token", MetadataCache(tmp_path / "cache.sqlite3"), client)
            for _ in range(2):
                assert {row.provider_id for row in await tmdb.search_movie("Dune", 2021)} == {"1", "2"}
        assert requests == [1, 2]
    asyncio.run(run())


@pytest.mark.parametrize("pages", [11, "2", -1, True])
def test_movie_search_refuses_incomplete_or_malformed_searches(tmp_path, pages):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"total_pages": pages, "results": []}))) as client:
            tmdb = TMDbProvider("token", MetadataCache(tmp_path / "cache.sqlite3"), client)
            with pytest.raises(ProviderError, match="uniqueness"):
                await tmdb.search_movie("Dune", 2021)
    asyncio.run(run())


def test_movie_details_require_requested_id(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": 99, "title": "Dune", "release_date": "2021-09-15"}))) as client:
            tmdb = TMDbProvider("token", MetadataCache(tmp_path / "cache.sqlite3"), client)
            with pytest.raises(ProviderError, match="details malformed"):
                await tmdb.get_movie("438631")
    asyncio.run(run())


def test_tmdb_series_external_ids_are_cached(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        assert request.url.path == "/3/tv/18347/external_ids"
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(200, json={"id": 18347, "imdb_id": "tt1439629", "tvdb_id": 94571})

    async def run():
        cache = MetadataCache(tmp_path / "cache.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tmdb = TMDbProvider("token", cache, client)
            assert await tmdb.get_series_external_ids("18347") == ("tt1439629", 94571)
            assert await tmdb.get_series_external_ids("18347") == ("tt1439629", 94571)
        assert len(requests) == 1
    asyncio.run(run())


def test_tmdb_season_episode_list_is_cached(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        assert request.url.path == "/3/tv/8592/season/7"
        return httpx.Response(200, json={"episodes": [
            {"episode_number": 11, "name": "Two Funerals"},
            {"episode_number": 12, "name": "One Last Ride"},
        ]})

    async def run():
        cache = MetadataCache(tmp_path / "cache.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tmdb = TMDbProvider("token", cache, client)
            expected = {11: "Two Funerals", 12: "One Last Ride"}
            assert await tmdb.get_series_season_episodes("8592", 7) == expected
            assert await tmdb.get_series_season_episodes("8592", 7) == expected
        assert len(requests) == 1
    asyncio.run(run())


def test_tvmaze_episode_list_includes_specials_and_is_cached(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        assert request.url.path == "/shows/538/episodes"
        assert request.url.params["specials"] == "1"
        return httpx.Response(200, json=[
            {"season": 11, "number": 1, "name": "The Impossible Stream"},
            {"season": None, "number": None, "name": "A Special"},
        ])

    async def run():
        cache = MetadataCache(tmp_path / "cache.sqlite3")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            maze = TVmazeProvider(cache, client)
            expected = [(11, 1, "The Impossible Stream"), (None, None, "A Special")]
            assert await maze.get_series_episodes("538") == expected
            assert await maze.get_series_episodes("538") == expected
        assert len(requests) == 1
    asyncio.run(run())
