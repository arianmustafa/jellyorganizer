"""Maintain labeled examples; provider snapshots do not establish real-world accuracy."""

import copy
import json
from pathlib import Path


def candidate(provider, identity, kind, title, year, **ids):
    return dict(provider=provider, provider_id=str(identity), kind=kind, title=title, year=year, **ids)


snapshots = {}
cases = []


def case(name, filename, kind, snapshot, expected=None, auto=False, **extra):
    cases.append(dict(name=name, filename=filename, kind=kind, snapshot=snapshot,
                      expected=expected, should_auto=auto, **extra))


for key, title, year, identity in (("dune", "Dune", 2021, 438631), ("arrival", "Arrival", 2016, 329865),
                                   ("we-live", "We Live in Time", 2024, 1100099)):
    entity = candidate("tmdb", identity, "movie", title, year)
    snapshots[key] = {"tmdb": [entity], "details": {str(identity): entity}}
    if key == "arrival":
        # Live TMDb contains a distinct 2016 short with this exact title.
        snapshots[key]["tmdb"].append(candidate("tmdb", 472349, "movie", "Arrival", 2016))
    releases = [f"{title.replace(' ', '.')}.{year}.1080p.BluRay.x265-GROUP.mkv",
                f"{title} ({year})/{title} ({year}).mkv",
                f"{title}.{year}.2160p.WEB-DL.DDP5.1.H.265.mkv",
                f"{title} ({year}) [tmdbid-{identity}]/{title}.mkv"]
    for index, filename in enumerate(releases):
        ambiguous_arrival = key == "arrival" and index != 3
        case(f"{key}-release-{index}", filename, "movie", key, {"tmdb_id": str(identity)},
             not ambiguous_arrival, live=ambiguous_arrival)
    case(f"{key}-no-year", f"{title}.mkv", "movie", key, {"tmdb_id": str(identity)})
    case(f"{key}-wrong-year", f"{title}.{year-1}.mkv", "movie", key)
    case(f"{key}-folder-conflict", f"Different Title ({year})/{title}.{year}.mkv", "movie", key)

ambiguous = copy.deepcopy(snapshots["dune"])
ambiguous["tmdb"].append(candidate("tmdb", 999, "movie", "Dune", 2021))
snapshots["ambiguous"] = ambiguous
case("same-title-year-two-identities", "Dune.2021.mkv", "movie", "ambiguous")
details_mismatch = copy.deepcopy(snapshots["dune"])
details_mismatch["details"]["438631"]["year"] = 1984
snapshots["details-mismatch"] = details_mismatch
case("search-details-disagreement", "Dune.2021.mkv", "movie", "details-mismatch")
case("wrong-remake", "Dune.1984.mkv", "movie", "dune")
case("fuzzy-movie-title", "Dun.2021.mkv", "movie", "dune")
case("ambiguous-shared-dump", "random.download.mkv", "auto", "dune")

for key, title, year, identity, maze_id, imdb, tvdb, episodes in (
    ("bear", "The Bear", 2022, 136315, 20, "tt14452776", 403294, {"2": {"3": "Sundae"}}),
    ("community", "Community", 2009, 18347, 318, "tt1439629", 94571, {"1": {"1": "Pilot"}}),
    ("robot", "Mr. Robot", 2015, 62560, 1871, "tt4158110", 289590,
     {"2": {"1": "eps2.0_unm4sk-pt1.tc", "2": "eps2.0_unm4sk-pt2.tc"}}),
):
    entity = candidate("tmdb", identity, "tv", title, year)
    snapshots[key] = {"tmdb": [entity], "details": {str(identity): entity}, "episodes": episodes,
                      "external_ids": [imdb, tvdb],
                      "tvmaze": [candidate("tvmaze", maze_id, "tv", title, year, imdb_id=imdb, tvdb_id=tvdb)],
                      "maze_episodes": [[int(season), int(number), name] for season, rows in episodes.items() for number, name in rows.items()]}

case("bear-year", "The Bear (2022)/The.Bear.S02E03.mkv", "tv", "bear", {"tmdb_id": "136315", "season": 2, "episodes": [3]}, True)
case("bear-episode-title", "The Bear - S02E03 - Sundae.mkv", "tv", "bear", {"tmdb_id": "136315", "season": 2, "episodes": [3]}, True)
case("community-yearless-ids", "Community.S01E01.REPACK.1080p.Bluray.x265-HiQVE.mkv", "tv", "community",
     {"tmdb_id": "18347", "season": 1, "episodes": [1]}, True)
case("robot-combined", "Mr Robot (2015)/Mr.Robot.S02E01-E02.1080p.BluRay.x265.mkv", "tv", "robot",
     {"tmdb_id": "62560", "season": 2, "episodes": [1, 2]}, True)
case("robot-range-missing-episode", "Mr Robot (2015)/Mr.Robot.S02E01-E03.mkv", "tv", "robot")
case("tv-missing-number", "Community.Pilot.mkv", "tv", "community")
case("tv-missing-episode", "Community.S01E99.mkv", "tv", "community")
case("tv-wrong-local-title", "The Bear (2022)/The Bear - S02E03 - A Different Episode.mkv", "tv", "bear")
case("tv-folder-season-conflict", "The Bear (2022)/Season 1/The.Bear.S02E03.mkv", "tv", "bear")

no_ids = copy.deepcopy(snapshots["community"])
no_ids.pop("external_ids")
no_ids["tvmaze"][0].pop("imdb_id")
no_ids["tvmaze"][0].pop("tvdb_id")
snapshots["no-ids"] = no_ids
case("yearless-no-ids", "Community.S01E01.mkv", "tv", "no-ids", {"tmdb_id": "18347", "season": 1, "episodes": [1]})
conflict = copy.deepcopy(snapshots["community"])
conflict["tvmaze"][0]["imdb_id"] = "tt9999999"
snapshots["conflicting-ids"] = conflict
case("contradictory-series-ids", "Community.S01E01.mkv", "tv", "conflicting-ids")

entity = candidate("tmdb", 8592, "tv", "Parks and Recreation", 2009)
snapshots["parks"] = {"tmdb": [entity], "details": {"8592": entity}, "external_ids": ["tt1266020", 84912],
                      "episodes": {"6": {"3": "Doppelgängers", "4": "Gin It Up!"}},
                      "tvmaze": [candidate("tvmaze", 174, "tv", "Parks and Recreation", 2009, imdb_id="tt1266020", tvdb_id=84912)],
                      "maze_episodes": [[6, 4, "Doppelgängers"], [6, 5, "Gin It Up!"]]}
case("parks-release-number-offset", "Parks and Recreation (2009)/Parks and Recreation - S06E04 - Doppelgängers.mkv", "tv", "parks",
     {"tmdb_id": "8592", "season": 6, "episodes": [3]}, True)
case("parks-title-needed", "Parks and Recreation (2009)/Parks.and.Recreation.S06E04.mkv", "tv", "parks")
case("parks-last-local-number", "Parks and Recreation (2009)/Parks and Recreation - S06E05 - Gin It Up!.mkv", "tv", "parks",
     {"tmdb_id": "8592", "season": 6, "episodes": [4]}, True)

corpus = {"version": 1, "name": "release-filenames-v1", "provenance": "Curated release-format examples from project regressions; frozen metadata fixtures, not an estimate of population accuracy.",
          "snapshots": snapshots, "cases": cases}
target = Path(__file__).resolve().parents[1] / "jellyorganize/resources/matching-corpus.json"
target.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n")
print(f"Wrote {len(cases)} labeled examples: {target}")
