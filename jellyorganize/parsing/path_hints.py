"""Extract stronger clues already present in directory structure."""

from __future__ import annotations

import re
from pathlib import Path


ID = re.compile(r"\[tmdbid-(\d+)\]", re.IGNORECASE)
TITLE_YEAR = re.compile(r"^(.+?)\s*\((19\d{2}|20\d{2})\)(?:\s*\[tmdbid-\d+\])?$", re.IGNORECASE)
SEASON = re.compile(r"^Season[ ._-]*(\d{1,2})$", re.IGNORECASE)


def path_hints(path: Path, root: Path, kind: str) -> dict:
    hints: dict = {}
    try:
        relatives = path.relative_to(root).parts[:-1]
    except ValueError:
        return hints
    for part in relatives:
        if match := ID.search(part):
            hints["tmdb_id"] = match.group(1)
        if match := TITLE_YEAR.match(part):
            hints["folder_title"] = match.group(1).strip()
            hints["folder_year"] = int(match.group(2))
        if kind == "tv" and (match := SEASON.match(part)):
            hints["folder_season"] = int(match.group(1))
    return hints
