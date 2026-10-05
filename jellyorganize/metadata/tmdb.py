"""TMDb v3 adapter. Bearer token is used only in request headers."""

from __future__ import annotations

import httpx

from jellyorganize.metadata.base import ProviderError, get_json
from jellyorganize.metadata.cache import MetadataCache
from jellyorganize.models import Candidate


BASE = "https://api.themoviedb.org/3"


class TMDbProvider:
    def __init__(self, token: str, cache: MetadataCache, client: httpx.AsyncClient):
        self._token = token
        self.cache = cache
        self.client = client

    async def _request(self, path: str, query: dict, *, days: int = 30,
                       entity_id: str | None = None) -> dict:
        key = {"path": path, **query}
        cached = self.cache.get("tmdb", key)
        if cached is not None:
            return cached
        if not self._token:
            raise ProviderError("TMDB_API_TOKEN is missing and this lookup is not cached")
        result = await get_json(self.client, BASE + path, params=query,
                                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"})
        if not isinstance(result, dict):
            raise ProviderError("malformed TMDb response")
        self.cache.put("tmdb", key, result,
                       entity_id=(entity_id or path.rsplit("/", 1)[-1]) if days == 180 else None, days=days)
        return result

    @staticmethod
    def _candidate(row: dict, kind: str) -> Candidate | None:
        title = row.get("title") if kind == "movie" else row.get("name")
        if not isinstance(title, str) or not title or not isinstance(row.get("id"), int) or row["id"] <= 0:
            return None
        date = row.get("release_date") if kind == "movie" else row.get("first_air_date")
        year = int(date[:4]) if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit() else None
        return Candidate(provider="tmdb", provider_id=str(row["id"]), kind=kind, title=title, year=year)

    async def search_movie(self, title: str, year: int | None = None) -> list[Candidate]:
        query = {"query": title}
        if year:
            query["year"] = year
        # An exact match on page one is not unique until every page is read.
        candidates = {}
        page, total_pages = 1, 1
        while page <= total_pages:
            data = await self._request("/search/movie", query if page == 1 else {**query, "page": page})
            rows = data.get("results")
            reported_pages = data.get("total_pages")
            if (not isinstance(rows, list) or type(reported_pages) is not int or
                    reported_pages < 0 or reported_pages > 10 or
                    (page > 1 and reported_pages != total_pages) or
                    (reported_pages == 0 and rows)):
                raise ProviderError("TMDb movie search is malformed or too broad to verify uniqueness")
            total_pages = reported_pages
            for row in rows:
                if isinstance(row, dict) and (candidate := self._candidate(row, "movie")):
                    candidates[candidate.provider_id] = candidate
            page += 1
        return list(candidates.values())

    async def search_series(self, title: str, year: int | None = None) -> list[Candidate]:
        query = {"query": title}
        if year:
            query["first_air_date_year"] = year
        data = await self._request("/search/tv", query)
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ProviderError("malformed TMDb series search response")
        return [candidate for row in rows if isinstance(row, dict) and (candidate := self._candidate(row, "tv"))]

    async def get_movie(self, movie_id: str) -> Candidate:
        data = await self._request(f"/movie/{movie_id}", {}, days=180)
        result = self._candidate(data, "movie")
        if result is None or result.provider_id != movie_id:
            raise ProviderError("TMDb movie details malformed")
        return result

    async def get_series(self, series_id: str) -> Candidate:
        data = await self._request(f"/tv/{series_id}", {}, days=180)
        result = self._candidate(data, "tv")
        if result is None or result.provider_id != series_id:
            raise ProviderError("TMDb series details malformed")
        return result

    async def get_series_external_ids(self, series_id: str) -> tuple[str | None, int | None]:
        """IMDb and TVDB identifiers, cached by TMDb series ID."""
        data = await self._request(f"/tv/{series_id}/external_ids", {}, days=180, entity_id=series_id)
        imdb = data.get("imdb_id")
        tvdb = data.get("tvdb_id")
        return (imdb if isinstance(imdb, str) and imdb.startswith("tt") and imdb[2:].isdigit() else None,
                tvdb if isinstance(tvdb, int) and not isinstance(tvdb, bool) and tvdb > 0 else None)

    async def get_series_episode(self, series_id: str, season: int, episode: int) -> str | None:
        try:
            data = await self._request(f"/tv/{series_id}/season/{season}/episode/{episode}", {}, days=180)
        except ProviderError as error:
            if str(error) == "provider entity not found":
                return None
            raise
        return data.get("name") if isinstance(data.get("name"), str) else None

    async def get_series_season_episodes(self, series_id: str, season: int) -> dict[int, str]:
        """Return a season by TMDb episode number for unique-title checks."""
        try:
            data = await self._request(f"/tv/{series_id}/season/{season}", {}, days=180,
                                       entity_id=series_id)
        except ProviderError as error:
            if str(error) == "provider entity not found":
                return {}
            raise
        rows = data.get("episodes")
        if not isinstance(rows, list):
            raise ProviderError("malformed TMDb season response")
        episodes: dict[int, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            number, title = row.get("episode_number"), row.get("name")
            if (isinstance(number, int) and not isinstance(number, bool) and number > 0 and
                    isinstance(title, str) and title.strip()):
                episodes[number] = title
        return episodes
