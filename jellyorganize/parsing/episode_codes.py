"""Episode numbers encoded in a filename, for duplicate safety checks."""

from __future__ import annotations

import re
from pathlib import Path


CODE = re.compile(r"\bS(?P<season>\d{1,2})E(?P<first>\d{1,3})(?P<rest>(?:-?E\d{1,3})*)\b", re.IGNORECASE)
NEXT = re.compile(r"(-?)E(\d{1,3})", re.IGNORECASE)


def coverage(path: Path) -> tuple[int, frozenset[int]] | None:
    """Return season and covered episodes for S01E01E02 or S01E01-E02."""
    match = CODE.search(path.stem)
    if match is None:
        return None
    season = int(match.group("season"))
    current = int(match.group("first"))
    episodes = {current}
    for next_match in NEXT.finditer(match.group("rest")):
        number = int(next_match.group(2))
        if next_match.group(1) == "-" and number > current:
            episodes.update(range(current + 1, number + 1))
        else:
            episodes.add(number)
        current = number
    return season, frozenset(episodes)


def overlaps(first: Path, second: Path) -> bool:
    left, right = coverage(first), coverage(second)
    return bool(left and right and left[0] == right[0] and left[1] & right[1])


def canonical_code(season: int, episodes: list[int]) -> str:
    if len(episodes) > 1 and episodes == list(range(episodes[0], episodes[-1] + 1)):
        return f"S{season:02d}E{episodes[0]:02d}-E{episodes[-1]:02d}"
    return f"S{season:02d}" + "".join(f"E{number:02d}" for number in episodes)
