"""GuessIt is one source of hints, not an identity decision."""

from __future__ import annotations

import re
from pathlib import Path

from guessit import guessit


FIELDS = ("title", "year", "season", "episode", "episode_title", "part", "language", "release_group", "source", "screen_size")
EPISODE_TITLE_AFTER_CODE = re.compile(r"\bS\d{1,2}E\d{1,3}(?:-?E\d{1,3})*\s*[-–—]\s*(.+)$", re.IGNORECASE)
BRACKETED_RELEASE_SUFFIX = re.compile(
    r"\s+[-–—]\s+\[(?=[^][]*(?:WEB|BluRay|HDTV|HDLight|DVDRip|REMUX|720p|1080p|2160p))[^][]+\]"
    r"(?:\s+\[[^][]+\])*$", re.IGNORECASE)
PARENTHESIZED_RELEASE_SUFFIX = re.compile(
    r"\s+\((?=[^()]*(?:720p|1080p|2160p))[^()]+\)$",
    re.IGNORECASE)


def parse_filename(path: Path, kind: str) -> dict:
    parsed = guessit(path.name, {"type": "episode" if kind == "tv" else "movie"})
    result = {key: parsed[key] for key in FIELDS if key in parsed}
    for key in ("year", "season"):
        if key in result:
            try:
                result[key] = int(result[key])
            except (ValueError, TypeError):
                result.pop(key)
    if "episode" in result:
        value = result["episode"]
        result["episode"] = [int(v) for v in value] if isinstance(value, list) else [int(value)]
    if kind == "tv" and (title_match := EPISODE_TITLE_AFTER_CODE.search(path.stem)):
        # GuessIt can interpret words in a real episode title as release
        # metadata ("History" is a known streaming service), and can drop a
        # numbered title suffix such as "(1)". A recognizable release suffix
        # marks where the real episode title ends.
        suffix = (BRACKETED_RELEASE_SUFFIX.search(title_match.group(1)) or
                  PARENTHESIZED_RELEASE_SUFFIX.search(title_match.group(1)))
        if suffix and (explicit_title := title_match.group(1)[:suffix.start()].strip()):
            result["episode_title"] = explicit_title
    return {key: str(value) if key in ("title", "episode_title") else value for key, value in result.items()}
