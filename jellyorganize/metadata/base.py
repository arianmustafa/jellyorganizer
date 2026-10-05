"""Provider contract and bounded HTTP retries."""

from __future__ import annotations

import asyncio
from typing import Protocol

import httpx

from jellyorganize.models import Candidate


class ProviderError(Exception):
    pass


class AuthenticationError(ProviderError):
    pass


class MetadataProvider(Protocol):
    async def search_movie(self, title: str, year: int | None = None) -> list[Candidate]: ...
    async def search_series(self, title: str, year: int | None = None) -> list[Candidate]: ...
    async def get_series_episode(self, series_id: str, season: int, episode: int) -> str | None: ...


async def get_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None, headers: dict | None = None) -> dict | list:
    for attempt in range(3):
        try:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code in (401, 403):
                raise AuthenticationError(f"provider authentication failed: HTTP {response.status_code}")
            if response.status_code == 404:
                raise ProviderError("provider entity not found")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, (dict, list)):
                raise ProviderError("malformed provider response")
            return value
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if attempt == 2:
                raise ProviderError(f"provider connection failed: {type(error).__name__}") from error
            await asyncio.sleep(min(2 ** attempt, 4))
        except (httpx.HTTPStatusError, ValueError) as error:
            raise ProviderError(f"provider response failed: {type(error).__name__}") from error
    raise ProviderError("provider request failed")
