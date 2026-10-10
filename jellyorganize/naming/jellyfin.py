"""Canonical paths proposed in memory; no filesystem operations."""

from __future__ import annotations

import re
from pathlib import Path

from jellyorganize.config import Config
from jellyorganize.models import Candidate, MediaItem
from jellyorganize.parsing.episode_codes import canonical_code
from jellyorganize.scanner.sidecars import sidecar_target_name


def safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .") or "Unknown"


def destinations(item: MediaItem, candidate: Candidate, config: Config, episode_title: str | None = None,
                 *, canonical_episodes: list[int] | None = None) -> tuple[Path, dict[Path, Path]]:
    title = safe_name(candidate.title)
    year = f" ({candidate.year})" if candidate.year else ""
    folder = title + year + (f" [tmdbid-{candidate.provider_id}]" if config.naming.include_tmdb_id else "")
    if item.kind == "movie":
        parent = config.movies.library / folder
        stem = title + year
        if config.naming.movie_versions:
            from jellyorganize.naming.movie_versions import label_for
            stem = folder + " - " + label_for(item)
    else:
        season = item.hints.get("season", item.hints.get("folder_season"))
        episodes = canonical_episodes if canonical_episodes is not None else item.hints.get("episode") or []
        if season is None or not episodes:
            raise ValueError("season and episode required")
        code = canonical_code(season, episodes)
        parent = config.tv.library / folder / f"Season {season:02d}"
        stem = f"{title} - {code}"
        if len(episodes) == 1 and config.naming.include_episode_title and episode_title:
            stem += f" - {safe_name(episode_title)}"
    target = parent / (stem + item.path.suffix)
    sidecars = {source: parent / sidecar_target_name(source, item.path, stem) for source in item.sidecars}
    return target, sidecars
