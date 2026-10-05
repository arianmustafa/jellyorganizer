"""Independent TV corroboration, never a replacement TMDb identity."""

from __future__ import annotations

import httpx

from jellyorganize.metadata.base import ProviderError, get_json
from jellyorganize.metadata.cache import MetadataCache
from jellyorganize.models import Candidate


BASE = "https://api.tvmaze.com"


class TVmazeProvider:
    def __init__(self, cache: MetadataCache, client: httpx.AsyncClient):
        self.cache = cache
        self.client = client

    async def _request(self, path: str, query: dict, *, days: int = 30,
                       entity_id: str | None = None) -> dict | list:
        key = {"path": path, **query}
        cached = self.cache.get("tvmaze", key)
        if cached is not None:
            return cached
        result = await get_json(self.client, BASE + path, params=query)
        self.cache.put("tvmaze", key, result,
                       entity_id=(entity_id or path.rsplit("/", 1)[-1]) if days == 180 else None, days=days)
        return result

    async def search_movie(self, title: str, year: int | None = None) -> list[Candidate]:
        return []

    async def search_series(self, title: str, year: int | None = None) -> list[Candidate]:
        data = await self._request("/search/shows", {"q": title})
        if not isinstance(data, list):
            raise ProviderError("malformed TVmaze search response")
        candidates = []
        for entry in data:
            show = entry.get("show", {}) if isinstance(entry, dict) else {}
            if not isinstance(show, dict) or not isinstance(show.get("id"), int) or show["id"] <= 0 or not isinstance(show.get("name"), str) or not show["name"]:
                continue
            date = show.get("premiered")
            shown_year = int(date[:4]) if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit() else None
            externals = show.get("externals") or {}
            if not isinstance(externals, dict):
                externals = {}
            imdb_id = externals.get("imdb") if isinstance(externals.get("imdb"), str) else None
            tvdb_id = externals.get("thetvdb") if isinstance(externals.get("thetvdb"), int) else None
            candidates.append(Candidate(provider="tvmaze", provider_id=str(show["id"]), kind="tv", title=show["name"], year=shown_year,
                                        imdb_id=imdb_id, tvdb_id=tvdb_id))
        return candidates

    async def get_series_episode(self, series_id: str, season: int, episode: int) -> str | None:
        try:
            data = await self._request(f"/shows/{series_id}/episodebynumber", {"season": season, "number": episode}, days=180)
        except ProviderError as error:
            if str(error) == "provider entity not found":
                return None
            raise
        return data.get("name") if isinstance(data, dict) and isinstance(data.get("name"), str) else None

    async def get_series_episodes(self, series_id: str) -> list[tuple[int | None, int | None, str]]:
        """Return all titles, including specials, for alternate-number checks."""
        data = await self._request(f"/shows/{series_id}/episodes", {"specials": 1},
                                   days=180, entity_id=series_id)
        if not isinstance(data, list):
            raise ProviderError("malformed TVmaze episode list")
        episodes = []
        for row in data:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
                continue
            season, number = row.get("season"), row.get("number")
            season = season if isinstance(season, int) and not isinstance(season, bool) and season >= 0 else None
            number = number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None
            episodes.append((season, number, row["name"]))
        return episodes
