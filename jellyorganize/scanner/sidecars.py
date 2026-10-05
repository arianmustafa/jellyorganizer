"""Recognize sidecars and retain subtitle suffixes."""

from __future__ import annotations

import re
from pathlib import Path


MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"}
SIDECAR_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".nfo", ".jpg", ".jpeg", ".png", ".webp"}
FOLDER_ART = re.compile(r"^(poster|fanart|logo|backdrop|banner|folder|season\d{1,2}-poster)$", re.I)
EPISODE = re.compile(r"s(\d{1,2})e(\d{1,3})", re.I)
LANGUAGES = {"en", "eng", "de", "deu", "ger", "fr", "fre", "fra", "es", "spa", "it", "ita", "pt", "por", "nl", "dut", "sv", "swe", "no", "nor", "da", "dan", "fi", "fin", "pl", "pol", "ru", "rus", "ja", "jpn", "ko", "kor", "zh", "zho", "chi", "ar", "ara", "hi", "hin", "tr", "tur", "cs", "cze", "el", "gre", "he", "heb", "hu", "hun", "ro", "rum", "uk", "ukr", "vi", "vie", "th", "tha", "id", "ind", "ms", "may"}
FLAGS = {"forced", "sdh", "hi", "default"}


def sidecar_owner(sidecar: Path, media: list[Path], kind: str) -> Path | None:
    if FOLDER_ART.match(sidecar.stem):
        return media[0] if len(media) == 1 else None
    direct = [item for item in media if sidecar.stem.casefold() == item.stem.casefold() or sidecar.stem.casefold().startswith(item.stem.casefold() + ".")]
    if len(direct) == 1:
        return direct[0]
    if kind == "tv":
        match = EPISODE.search(sidecar.stem)
        if match:
            found = [item for item in media if (m := EPISODE.search(item.stem)) and m.groups() == match.groups()]
            return found[0] if len(found) == 1 else None
        # An episode sidecar without an episode code is too ambiguous to pair
        # with a release merely because it is the only video in the folder.
        return None
    if len(media) == 1:
        tokens = re.split(r"[._ -]+", sidecar.stem)
        while tokens and tokens[-1].casefold() in LANGUAGES | FLAGS:
            tokens.pop()
        base = re.sub(r"[^a-z0-9]+", "", "".join(tokens).casefold())
        video = re.sub(r"[^a-z0-9]+", "", media[0].stem.casefold())
        if len(base) >= 4 and video.startswith(base):
            return media[0]
    return None


def sidecar_target_name(sidecar: Path, media: Path, target_stem: str) -> str:
    if FOLDER_ART.match(sidecar.stem):
        return sidecar.name
    suffix = sidecar.stem[len(media.stem):] if sidecar.stem.casefold().startswith(media.stem.casefold()) else ""
    if suffix and not suffix.startswith("."):
        suffix = ""
    if not suffix:
        tokens = re.split(r"[._ -]+", sidecar.stem)
        flags = []
        while tokens and tokens[-1].casefold() in FLAGS:
            flags.insert(0, tokens.pop())
        if tokens and tokens[-1].casefold() in LANGUAGES:
            flags.insert(0, tokens[-1])
        suffix = "." + ".".join(flags) if flags else ""
    return f"{target_stem}{suffix}{sidecar.suffix}"
