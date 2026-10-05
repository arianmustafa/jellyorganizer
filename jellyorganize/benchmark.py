"""Reproducible matching measurements against labeled release filenames."""

import asyncio
import json
from importlib.resources import files
from pathlib import Path

import httpx

from jellyorganize.config import Config
from jellyorganize.metadata.base import ProviderError
from jellyorganize.metadata.cache import MetadataCache
from jellyorganize.metadata.matcher import resolve
from jellyorganize.metadata.tmdb import TMDbProvider
from jellyorganize.metadata.tvmaze import TVmazeProvider
from jellyorganize.models import Candidate, MediaItem
from jellyorganize.parsing.guessit_parser import parse_filename
from jellyorganize.parsing.path_hints import path_hints
from jellyorganize.scanner.packages import detect_kind
from jellyorganize.service import token


class SnapshotTMDb:
    def __init__(self, snapshot):
        self.data = snapshot

    async def search_movie(self, title, year=None):
        return [Candidate.model_validate(row) for row in self.data.get("tmdb", [])]

    search_series = search_movie

    async def get_movie(self, entity_id):
        row = self.data.get("details", {}).get(entity_id)
        if row is None:
            raise ProviderError("provider entity not found")
        return Candidate.model_validate(row)

    get_series = get_movie

    async def get_series_episode(self, entity_id, season, episode):
        return self.data.get("episodes", {}).get(str(season), {}).get(str(episode))

    async def get_series_season_episodes(self, entity_id, season):
        return {int(number): name for number, name in self.data.get("episodes", {}).get(str(season), {}).items()}

    async def get_series_external_ids(self, entity_id):
        return tuple(self.data.get("external_ids", [None, None]))


class SnapshotTVmaze:
    def __init__(self, snapshot):
        self.data = snapshot

    async def search_series(self, title, year=None):
        return [Candidate.model_validate(row) for row in self.data.get("tvmaze", [])]

    async def get_series_episodes(self, entity_id):
        return [tuple(row) for row in self.data.get("maze_episodes", [])]

    async def get_series_episode(self, entity_id, season, episode):
        return next((row[2] for row in self.data.get("maze_episodes", []) if row[:2] == [season, episode]), None)


def load_corpus(path=None):
    data = json.loads(Path(path).read_text() if path else files("jellyorganize.resources").joinpath("matching-corpus.json").read_text())
    if data.get("version") != 1 or not isinstance(data.get("cases"), list) or not data["cases"]:
        raise ValueError("matching corpus must contain version 1 and nonempty labeled cases")
    names = set()
    for case in data["cases"]:
        if not isinstance(case.get("name"), str) or case["name"] in names:
            raise ValueError("corpus cases need unique names")
        names.add(case["name"])
        if not isinstance(case.get("filename"), str) or case.get("kind") not in {"movie", "tv", "auto"}:
            raise ValueError("each case needs a filename and a movie, tv, or auto kind")
        if type(case.get("should_auto")) is not bool:
            raise ValueError("each case needs an explicit should_auto label")
        if case["should_auto"] and not isinstance(case.get("expected"), dict):
            raise ValueError("automatic cases need an independently supplied expected identity")
    return data


async def evaluate(config=None, corpus_path=None, *, live=False):
    config = config or Config()
    corpus = load_corpus(corpus_path)
    results = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        tmdb_live = TMDbProvider(token(config), MetadataCache(config.cache_path), client) if live else None
        maze_live = TVmazeProvider(MetadataCache(config.cache_path), client) if live else None
        for case in corpus["cases"]:
            # Synthetic provider contradictions belong to the offline suite.
            # Live checks measure metadata agreement on labeled positive cases.
            if live and not case["should_auto"] and not case.get("live", False):
                continue
            root = Path("/benchmark/Incoming")
            path = root / case["filename"]
            kind = detect_kind(path, root) if case["kind"] == "auto" else case["kind"]
            candidate, confidence, reason, episodes = None, 0, "media type is ambiguous", None
            hints = {}
            if kind:
                hints = {**parse_filename(path, kind), **path_hints(path, root, kind), **case.get("hints", {})}
                item = MediaItem(path=path, root=root, kind=kind, hints=hints)
                snapshot = corpus.get("snapshots", {}).get(case.get("snapshot"), {})
                tmdb = tmdb_live if live else SnapshotTMDb(snapshot)
                maze = maze_live if live else SnapshotTVmaze(snapshot)
                candidate, confidence, reason, _, _, episodes = await resolve(
                    item, tmdb, maze if config.providers.tvmaze else None,
                    confirm_exact_movies=config.matching.confirm_exact_movies)
            accepted = bool(candidate and candidate.year and candidate.provider == "tmdb" and
                            confidence >= max(0.97, config.matching.auto_apply_threshold))
            actual = ({"tmdb_id": candidate.provider_id} if accepted else None)
            if accepted and kind == "tv":
                actual.update(season=hints.get("season", hints.get("folder_season")),
                              episodes=episodes if episodes is not None else hints.get("episode"))
            correct = actual == case.get("expected") if accepted else True
            results.append({"name": case["name"], "filename": case["filename"], "accepted": accepted,
                            "actual": actual, "expected": case.get("expected"), "correct": correct,
                            "expected_auto": case["should_auto"], "score": confidence, "reason": reason})
    accepted = sum(row["accepted"] for row in results)
    wrong = sum(row["accepted"] and not row["correct"] for row in results)
    missed = sum(row["expected_auto"] and not row["accepted"] for row in results)
    unexpected = sum(not row["expected_auto"] and row["accepted"] for row in results)
    return {"corpus": corpus.get("name", "custom"), "mode": "live-provider labeled examples" if live else "frozen-metadata regression",
            "cases": len(results), "automatic_matches": accepted, "wrong_automatic_matches": wrong,
            "missed_expected_automatic_matches": missed,
            "unexpected_automatic_matches": unexpected,
            "automatic_coverage": accepted / len(results), "precision_on_this_corpus": (accepted - wrong) / accepted if accepted else None,
            "passed": wrong == 0 and missed == 0 and unexpected == 0, "results": results}


if __name__ == "__main__":
    result = asyncio.run(evaluate())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
