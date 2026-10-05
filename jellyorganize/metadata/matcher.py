"""Conservative identity decisions. A search result alone is insufficient."""

from __future__ import annotations

import re
import unicodedata

from jellyorganize.metadata.base import ProviderError
from jellyorganize.models import Candidate, MediaItem
from jellyorganize.parsing.episode_codes import canonical_code


MULTI_CODE = re.compile(r"\bS\d{1,2}E\d{1,3}(?:-?E\d{1,3})+\b", re.IGNORECASE)
PART_TITLE = re.compile(
    r"(?P<base>.+?)(?:\s*\(\s*(?:part\s*)?(?P<bracket>\d+)\s*\)|\s*[-,:]?\s+part\s*(?P<plain>\d+))$",
    re.IGNORECASE)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(character for character in value if not unicodedata.combining(character))
    return "".join(character for character in value if character.isalnum())


def shared_part_title(titles: list[str]) -> str | None:
    """Return the base title only for distinct, sequentially numbered parts."""
    base = None
    for position, title in enumerate(titles, 1):
        match = PART_TITLE.fullmatch(title)
        if match is None or int(match.group("bracket") or match.group("plain")) != position:
            return None
        part_base = match.group("base").strip()
        if not normalize(part_base) or (base is not None and normalize(base) != normalize(part_base)):
            return None
        base = part_base
    return base


def episode_titles_correspond(local: str, tmdb: str, tvmaze: str, series: str) -> bool:
    """Accept a provider's omission of a redundant series-name prefix."""
    canonical = normalize(tmdb)
    if not canonical or normalize(local) != canonical:
        return False
    def provider_matches(title: str) -> bool:
        if normalize(tvmaze) == normalize(title):
            return True
        # Some catalogs omit a trailing part number in the same numbered
        # episode's title, e.g. TMDb "Aliens of London (1)" vs TVmaze
        # "Aliens of London". The local title must still equal TMDb above.
        unnumbered = re.sub(r"\s*\([1-9]\d*\)\s*$", "", title)
        return unnumbered != title and normalize(unnumbered) == normalize(tvmaze)

    if provider_matches(tmdb):
        return True
    prefix = re.fullmatch(rf"{re.escape(series)}\s*[:\-–—]\s*(.+)", tmdb, re.IGNORECASE)
    return bool(prefix and normalize(prefix.group(1)) and provider_matches(prefix.group(1)))


def external_id_agreement(tmdb_imdb: str | None, tmdb_tvdb: int | None,
                          maze_imdb: str | None, maze_tvdb: int | None) -> str:
    """Return match, conflict, or absent using only IDs present on both sides."""
    comparisons = []
    if tmdb_imdb and maze_imdb:
        comparisons.append(tmdb_imdb.casefold() == maze_imdb.casefold())
    if tmdb_tvdb and maze_tvdb:
        comparisons.append(tmdb_tvdb == maze_tvdb)
    if False in comparisons:
        return "conflict"
    return "match" if comparisons else "absent"


def local_title_year(item: MediaItem) -> tuple[str | None, int | None]:
    title = item.hints.get("folder_title") or item.hints.get("title") or item.hints.get("package_title")
    year = item.hints.get("folder_year") or item.hints.get("year") or item.hints.get("package_year")
    return (str(title) if title else None, int(year) if year else None)


async def _resolve_base(item: MediaItem, tmdb, tvmaze=None, identities=None, *, confirm_exact_movies=False) -> tuple[Candidate | None, float, str, list[Candidate], str | None]:
    """Return candidate, confidence, reason, alternatives, episode title."""
    title, year = local_title_year(item)
    explicit_id = item.hints.get("tmdb_id")
    stored_candidate = None
    if identities is not None and not explicit_id:
        stored_candidate, _ = identities.lookup(item)
    if not explicit_id and not stored_candidate and item.hints.get("folder_year") and item.hints.get("year") and item.hints["folder_year"] != item.hints["year"]:
        return None, 0, "folder year conflicts with filename", [], None
    if not explicit_id and not stored_candidate and item.hints.get("package_year") and item.hints.get("year") and item.hints["package_year"] != item.hints["year"]:
        return None, 0, "package year conflicts with filename", [], None
    if not title and not explicit_id and not stored_candidate:
        return None, 0, "no usable title", [], None
    if not explicit_id and not stored_candidate and item.kind == "movie":
        filename_title = item.hints.get("title")
        if item.hints.get("folder_title") and filename_title and normalize(str(filename_title)) != normalize(title):
            return None, 0, "folder title conflicts with filename", [], None
    try:
        if explicit_id:
            candidate = await (tmdb.get_movie(explicit_id) if item.kind == "movie" else tmdb.get_series(explicit_id))
            choices = [candidate]
            confidence = 1.0
            reason = "explicit TMDb ID verified"
        elif stored_candidate:
            candidate = stored_candidate
            choices = [candidate]
            confidence = 1.0
            reason = "persisted manual identity"
        else:
            choices = await (tmdb.search_movie(title, year) if item.kind == "movie" else tmdb.search_series(title, year))
            exact = [candidate for candidate in choices if normalize(title) and normalize(candidate.title) == normalize(title) and (year is None or candidate.year == year)]
            if len(exact) != 1:
                return None, 0.0, "ambiguous or unmatched TMDb search", choices[:5], None
            candidate = exact[0]
            confidence = 0.90 if year is not None else 0.75
            reason = "exact TMDb title and year; single provider" if year else "title only; year absent"
            if item.kind == "movie" and year is not None and confirm_exact_movies:
                verified = await tmdb.get_movie(candidate.provider_id)
                if (verified.provider != "tmdb" or verified.kind != "movie" or
                        verified.provider_id != candidate.provider_id or verified.year != year or
                        normalize(verified.title) != normalize(title)):
                    return candidate, 0.70, "TMDb movie search and details disagree", choices[:5], None
                candidate = verified
                confidence = 0.98
                reason = "unique exact movie title and year; TMDb details verified"

        episode_title = None
        if item.kind == "tv":
            season = item.hints.get("season", item.hints.get("folder_season"))
            episodes = item.hints.get("episode") or []
            if season is None or not episodes:
                return candidate, 0.0, "episode number or season missing", choices[:5], None
            if item.hints.get("folder_season") is not None and item.hints.get("season") is not None and item.hints["folder_season"] != item.hints["season"]:
                return candidate, 0.0, "season folder conflicts with filename", choices[:5], None
            if len(episodes) > 1 and (len(episodes) > 12 or any(number < 1 for number in episodes) or
                                      episodes != list(range(episodes[0], episodes[-1] + 1))):
                return candidate, min(confidence, 0.80), "multi-episode numbers are not a short contiguous range", choices[:5], None
            titles = [await tmdb.get_series_episode(candidate.provider_id, season, episode) for episode in episodes]
            if any(name is None for name in titles):
                return candidate, 0.0, "TMDb episode missing", choices[:5], None
            if len(episodes) > 1:
                if not title or normalize(title) != normalize(candidate.title) or (year is not None and year != candidate.year):
                    return candidate, min(confidence, 0.80), "multi-episode series title or year disagrees", choices[:5], None
                if tvmaze is None:
                    return candidate, min(confidence, 0.80), "multi-episode file needs TVmaze corroboration", choices[:5], None
                if item.hints.get("episode_title"):
                    code = MULTI_CODE.search(item.path.stem)
                    tail = normalize(item.path.stem[code.end():]) if code else ""
                    full_titles_present = all(normalize(name) and normalize(name) in tail for name in titles)
                    common_title = shared_part_title(titles)
                    if not full_titles_present and not (common_title and normalize(str(item.hints["episode_title"])) == normalize(common_title)):
                        return candidate, min(confidence, 0.80), "multi-episode filename titles do not match TMDb", choices[:5], None
                maze = await tvmaze.search_series(candidate.title, candidate.year)
                matching = [row for row in maze if normalize(row.title) == normalize(candidate.title) and row.year == candidate.year]
                if len(matching) != 1:
                    return candidate, min(confidence, 0.80), "TVmaze did not corroborate multi-episode series", choices[:5] + maze[:3], None
                maze_titles = [await tvmaze.get_series_episode(matching[0].provider_id, season, episode) for episode in episodes]
                if any(name is None for name in maze_titles):
                    return candidate, min(confidence, 0.80), "TVmaze multi-episode number missing", choices[:5], None
                if not all(episode_titles_correspond(tmdb_title, tmdb_title, maze_title, candidate.title)
                           for tmdb_title, maze_title in zip(titles, maze_titles, strict=True)):
                    return candidate, min(confidence, 0.80), "TMDb and TVmaze multi-episode titles disagree", choices[:5], None
                tmdb_imdb, tmdb_tvdb = await tmdb.get_series_external_ids(candidate.provider_id)
                ids_agree = external_id_agreement(tmdb_imdb, tmdb_tvdb, matching[0].imdb_id, matching[0].tvdb_id)
                if ids_agree != "match":
                    reason = ("TMDb and TVmaze external IDs disagree" if ids_agree == "conflict"
                              else "multi-episode series external IDs unavailable")
                    return candidate, min(confidence, 0.80), reason, choices[:5], None
                candidate = candidate.model_copy(update={"tvmaze_id": matching[0].provider_id,
                                                   "imdb_id": matching[0].imdb_id,
                                                   "tvdb_id": matching[0].tvdb_id})
                return candidate, 0.99, f"TMDb and TVmaze corroborate {canonical_code(season, episodes)} and series IDs", choices[:5], None
            episode_title = titles[0]
            local_episode_title = item.hints.get("episode_title")
            if local_episode_title and normalize(str(local_episode_title)) != normalize(episode_title):
                return candidate, min(confidence, 0.85), "local episode title disagrees with TMDb", choices[:5], episode_title
            if tvmaze is not None and not explicit_id and not stored_candidate:
                maze = await tvmaze.search_series(title, year)
                matching = [row for row in maze if normalize(row.title) == normalize(candidate.title) and row.year == candidate.year]
                contradictory = [row for row in maze if normalize(row.title) == normalize(title) and row.year is not None and row.year != candidate.year]
                # Search can return several versions of a series. A different
                # year matters when the local files have no year, or when the
                # expected version is absent; it does not refute an exact
                # local-year match (for example Doctor Who 1963 and 2005).
                if contradictory and len(matching) != 1:
                    return candidate, min(confidence, 0.70), "TVmaze year disagrees with TMDb", choices[:5] + contradictory[:3], episode_title
                if len(matching) != 1:
                    return candidate, min(confidence, 0.85), "TVmaze did not corroborate series", choices[:5] + maze[:3], episode_title
                maze_episode = await tvmaze.get_series_episode(matching[0].provider_id, season, episodes[0])
                same_number_agrees = bool(maze_episode and episode_titles_correspond(
                    episode_title, episode_title, maze_episode, candidate.title))
                if not same_number_agrees:
                    if not local_episode_title:
                        reason = "TVmaze episode missing" if maze_episode is None else "TVmaze same-number episode title differs"
                        return candidate, min(confidence, 0.85), reason, choices[:5], episode_title
                    maze_episodes = await tvmaze.get_series_episodes(matching[0].provider_id)
                    alternate = [row for row in maze_episodes if episode_titles_correspond(
                        str(local_episode_title), episode_title, row[2], candidate.title)]
                    if len(alternate) != 1:
                        reason = "TVmaze alternate episode title missing or ambiguous"
                        return candidate, min(confidence, 0.85), reason, choices[:5], episode_title
                    tmdb_imdb, tmdb_tvdb = await tmdb.get_series_external_ids(candidate.provider_id)
                    ids_agree = external_id_agreement(tmdb_imdb, tmdb_tvdb,
                                                      matching[0].imdb_id, matching[0].tvdb_id)
                    if ids_agree != "match":
                        reason = ("TMDb and TVmaze external IDs disagree" if ids_agree == "conflict"
                                  else "TVmaze alternate numbering; external IDs unavailable")
                        return candidate, min(confidence, 0.85), reason, choices[:5], episode_title
                    alternate_season, alternate_number, _ = alternate[0]
                    alternate_code = (f"S{alternate_season:02d}E{alternate_number:02d}"
                                      if alternate_season is not None and alternate_number is not None else "special")
                    candidate = candidate.model_copy(update={"tvmaze_id": matching[0].provider_id,
                                                       "imdb_id": matching[0].imdb_id,
                                                       "tvdb_id": matching[0].tvdb_id})
                    return candidate, 0.99, f"TMDb S{season:02d}E{episodes[0]:02d} matches TVmaze {alternate_code} by title and series IDs", choices[:5], episode_title
                candidate = candidate.model_copy(update={"tvmaze_id": matching[0].provider_id,
                                                   "imdb_id": matching[0].imdb_id,
                                                   "tvdb_id": matching[0].tvdb_id})
                episode_titles_agree = bool(local_episode_title and episode_title and maze_episode and
                                            episode_titles_correspond(str(local_episode_title), episode_title,
                                                                      maze_episode, candidate.title))
                ids_agree = "absent"
                if year is None and (not episode_titles_agree or contradictory):
                    tmdb_imdb, tmdb_tvdb = await tmdb.get_series_external_ids(candidate.provider_id)
                    ids_agree = external_id_agreement(tmdb_imdb, tmdb_tvdb, matching[0].imdb_id, matching[0].tvdb_id)
                    if ids_agree == "conflict":
                        return candidate, 0.70, "TMDb and TVmaze external IDs disagree", choices[:5], episode_title
                if year is None and contradictory and ids_agree != "match":
                    return candidate, min(confidence, 0.70), "TVmaze year disagrees with TMDb", choices[:5] + contradictory[:3], episode_title
                confidence = 0.99 if year is not None or episode_titles_agree or ids_agree == "match" else 0.95
                reason = ("TMDb and TVmaze title, year, and episode agree" if year is not None else
                          "local episode title agrees with TMDb and TVmaze" if episode_titles_agree else
                          "TMDb and TVmaze external IDs agree" if ids_agree == "match" else
                          "TMDb and TVmaze agree; local year absent")
        return candidate, confidence, reason, choices[:5], episode_title
    except ProviderError as error:
        return None, 0.0, f"provider error: {error}", [], None


async def _resolve_single_numbering(item, candidate, confidence, reason, choices, episode_title, tmdb, tvmaze):
    """Map a release's number only when both catalogs identify its local title."""
    fallback = candidate, confidence, reason, choices, episode_title, None
    season = item.hints.get("season", item.hints.get("folder_season"))
    local_title = item.hints.get("episode_title")
    title, year = local_title_year(item)
    if (season is None or not local_title or not normalize(str(local_title)) or not title or
            normalize(title) != normalize(candidate.title) or (year is not None and year != candidate.year)):
        return fallback
    try:
        maze = await tvmaze.search_series(candidate.title, candidate.year)
        matching = [row for row in maze if normalize(row.title) == normalize(candidate.title) and row.year == candidate.year]
        if len(matching) != 1:
            return fallback
        tmdb_imdb, tmdb_tvdb = await tmdb.get_series_external_ids(candidate.provider_id)
        if external_id_agreement(tmdb_imdb, tmdb_tvdb, matching[0].imdb_id, matching[0].tvdb_id) != "match":
            return fallback
        catalog = await tmdb.get_series_season_episodes(candidate.provider_id, season)
        targets = [number for number, name in catalog.items() if normalize(name) == normalize(str(local_title))]
        if len(targets) != 1:
            return fallback
        maze_episodes = await tvmaze.get_series_episodes(matching[0].provider_id)
        anchors = [row for row in maze_episodes if episode_titles_correspond(
            str(local_title), catalog[targets[0]], row[2], candidate.title)]
        if len(anchors) != 1 or anchors[0][:2] != (season, item.hints["episode"][0]):
            return fallback
        candidate = candidate.model_copy(update={"tvmaze_id": matching[0].provider_id,
                                                "imdb_id": matching[0].imdb_id, "tvdb_id": matching[0].tvdb_id})
        source_code = canonical_code(season, item.hints["episode"])
        target_code = canonical_code(season, targets)
        return (candidate, 0.99,
                f"TVmaze {source_code} maps to TMDb {target_code}; unique local episode title and series IDs agree",
                choices, catalog[targets[0]], targets)
    except ProviderError as error:
        return None, 0.0, f"provider error: {error}", [], None, None


async def resolve(item: MediaItem, tmdb, tvmaze=None, identities=None, *, confirm_exact_movies=False) -> tuple[
    Candidate | None, float, str, list[Candidate], str | None, list[int] | None
]:
    """Resolve identity and optional provider-order episode mapping."""
    candidate, confidence, reason, choices, episode_title = await _resolve_base(
        item, tmdb, tvmaze, identities, confirm_exact_movies=confirm_exact_movies)
    episodes = item.hints.get("episode") or []
    if (item.kind == "tv" and tvmaze is not None and candidate is not None and len(episodes) == 1 and
            reason in {"local episode title disagrees with TMDb", "TMDb episode missing"}):
        return await _resolve_single_numbering(item, candidate, confidence, reason, choices, episode_title, tmdb, tvmaze)
    if (item.kind != "tv" or tvmaze is None or candidate is None or len(episodes) != 2 or
            reason not in {"multi-episode filename titles do not match TMDb",
                           "TMDb and TVmaze multi-episode titles disagree",
                           "TMDb episode missing"}):
        return candidate, confidence, reason, choices, episode_title, None

    season = item.hints.get("season", item.hints.get("folder_season"))
    local_title = item.hints.get("episode_title")
    if season is None or not local_title or episodes[1] != episodes[0] + 1:
        return candidate, confidence, reason, choices, episode_title, None
    title, year = local_title_year(item)
    if not title or normalize(title) != normalize(candidate.title) or year != candidate.year:
        return candidate, confidence, reason, choices, episode_title, None
    try:
        if reason == "TMDb episode missing":
            # A release may number a two-part season finale as two episodes,
            # while TMDb catalogs the combined finale once. Require the
            # preceding episode and both season endings to anchor the mapping.
            maze = await tvmaze.search_series(candidate.title, candidate.year)
            matching = [row for row in maze if normalize(row.title) == normalize(candidate.title)
                        and row.year == candidate.year]
            if len(matching) != 1 or episodes[0] < 2:
                return candidate, confidence, reason, choices, episode_title, None
            maze_id = matching[0].provider_id
            maze_titles = [await tvmaze.get_series_episode(maze_id, season, number)
                           for number in (episodes[0] - 1, episodes[0], episodes[1], episodes[1] + 1)]
            if (not maze_titles[0] or maze_titles[3] is not None or
                    any(name is None for name in maze_titles[1:3]) or
                    normalize(shared_part_title(maze_titles[1:3]) or "") != normalize(str(local_title))):
                return candidate, confidence, reason, choices, episode_title, None
            tmdb_season = await tmdb.get_series_season_episodes(candidate.provider_id, season)
            matches = [number for number, name in tmdb_season.items()
                       if normalize(name) == normalize(str(local_title))]
            if (len(matches) != 1 or matches[0] not in {episodes[0], episodes[0] - 1} or
                    matches[0] != max(tmdb_season, default=0) or
                    not (previous_tmdb := tmdb_season.get(matches[0] - 1)) or
                    not episode_titles_correspond(previous_tmdb, previous_tmdb,
                                                  maze_titles[0], candidate.title)):
                return candidate, confidence, reason, choices, episode_title, None
            tmdb_imdb, tmdb_tvdb = await tmdb.get_series_external_ids(candidate.provider_id)
            if external_id_agreement(tmdb_imdb, tmdb_tvdb,
                                     matching[0].imdb_id, matching[0].tvdb_id) != "match":
                return candidate, confidence, reason, choices, episode_title, None
            candidate = candidate.model_copy(update={"tvmaze_id": maze_id,
                                               "imdb_id": matching[0].imdb_id,
                                               "tvdb_id": matching[0].tvdb_id})
            source_code = canonical_code(season, episodes)
            target_code = canonical_code(season, matches)
            return (candidate, 0.99,
                    f"TVmaze {source_code} maps to TMDb {target_code}; finale, previous episode, and series IDs agree",
                    choices, tmdb_season[matches[0]], matches)

        first_tmdb = await tmdb.get_series_episode(candidate.provider_id, season, episodes[0])
        next_tmdb = await tmdb.get_series_episode(candidate.provider_id, season, episodes[1])
        if not first_tmdb or not next_tmdb or normalize(str(local_title)) != normalize(first_tmdb) or normalize(first_tmdb) == normalize(next_tmdb):
            return candidate, confidence, reason, choices, episode_title, None
        maze = await tvmaze.search_series(candidate.title, candidate.year)
        matching = [row for row in maze if normalize(row.title) == normalize(candidate.title) and row.year == candidate.year]
        if len(matching) != 1:
            return candidate, confidence, reason, choices, episode_title, None
        maze_titles = [await tvmaze.get_series_episode(matching[0].provider_id, season, number)
                       for number in (episodes[0], episodes[1], episodes[1] + 1)]
        if (any(name is None for name in maze_titles) or
                normalize(shared_part_title(maze_titles[:2]) or "") != normalize(str(local_title)) or
                not episode_titles_correspond(next_tmdb, next_tmdb, maze_titles[2], candidate.title)):
            return candidate, confidence, reason, choices, episode_title, None
        tmdb_imdb, tmdb_tvdb = await tmdb.get_series_external_ids(candidate.provider_id)
        if external_id_agreement(tmdb_imdb, tmdb_tvdb, matching[0].imdb_id, matching[0].tvdb_id) != "match":
            return candidate, confidence, reason, choices, episode_title, None
        candidate = candidate.model_copy(update={"tvmaze_id": matching[0].provider_id,
                                           "imdb_id": matching[0].imdb_id,
                                           "tvdb_id": matching[0].tvdb_id})
        source_code = canonical_code(season, episodes)
        target_code = canonical_code(season, [episodes[0]])
        return (candidate, 0.99,
                f"TVmaze {source_code} maps to TMDb {target_code}; next episode title and series IDs agree",
                choices, first_tmdb, [episodes[0]])
    except ProviderError as error:
        return None, 0.0, f"provider error: {error}", [], None, None
