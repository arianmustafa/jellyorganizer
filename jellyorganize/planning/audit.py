"""Read-only repair proposals for media already in a Jellyfin library."""

from __future__ import annotations

import re
from pathlib import Path

from jellyorganize.config import Config
from jellyorganize.metadata.matcher import resolve
from jellyorganize.models import MediaItem, Proposal, ScanResult
from jellyorganize.naming.jellyfin import destinations, safe_name
from jellyorganize.parsing.episode_codes import canonical_code, coverage, overlaps
from jellyorganize.scanner.sidecars import MEDIA_EXTENSIONS, sidecar_target_name


FOLDER = re.compile(r"^(.+?) \((19\d{2}|20\d{2})\) \[tmdbid-(\d+)\]$")


def already_canonical(item: MediaItem, config: Config) -> bool:
    """Trust a complete local provider-ID layout without a network lookup."""
    if not config.naming.include_tmdb_id:
        return False
    relative = item.path.relative_to(item.root)
    parts = relative.parts
    if item.kind == "movie":
        if len(parts) != 2 or not (match := FOLDER.fullmatch(parts[0])):
            return False
        title, year, _ = match.groups()
        if safe_name(title) != title:
            return False
        if config.naming.movie_versions:
            from jellyorganize.naming.movie_versions import version_of
            if version_of(item.path) is None:
                return False
        elif item.path.stem != f"{title} ({year})":
            return False
    else:
        if len(parts) != 3 or not (match := FOLDER.fullmatch(parts[0])):
            return False
        title, _, _ = match.groups()
        if safe_name(title) != title:
            return False
        season = item.hints.get("season", item.hints.get("folder_season"))
        episodes = item.hints.get("episode") or []
        if season is None or not episodes or parts[1] != f"Season {season:02d}":
            return False
        code = canonical_code(season, episodes)
        stem = f"{title} - {code}"
        if len(episodes) > 1:
            if item.path.stem != stem:
                return False
        elif config.naming.include_episode_title:
            if not item.path.stem.startswith(stem + " - ") or not item.path.stem[len(stem) + 3:]:
                return False
        elif item.path.stem != stem:
            return False
    sidecars = {sidecar: sidecar for sidecar in item.sidecars}
    if not all(sidecar.parent == item.path.parent and
               sidecar.name == sidecar_target_name(sidecar, item.path, item.path.stem)
               for sidecar in item.sidecars):
        return False
    return not _conflict(Proposal(item, "SKIP", "already canonical", destination=item.path,
                                  sidecar_destinations=sidecars))


def _conflict(proposal: Proposal) -> bool:
    """Check occupied destinations, ignoring only this item's unchanged paths."""
    destination = proposal.destination
    if destination is None:
        return False
    files = [(proposal.item.path, destination), *proposal.sidecar_destinations.items()]
    if len({target for _, target in files}) != len(files):
        return True
    for source, target in files:
        if source != target and (target.exists() or target.is_symlink()):
            return True
    parent = destination.parent
    if not parent.exists():
        return False
    if parent.is_symlink() or not parent.is_dir():
        return True
    if proposal.item.kind == "movie":
        from jellyorganize.naming.movie_versions import movie_conflict
        return movie_conflict(destination, proposal.item.path)
    for path in parent.iterdir():
        if path == proposal.item.path or not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        target_coverage = coverage(destination)
        if overlaps(destination, path) or (target_coverage and len(target_coverage[1]) > 1 and coverage(path) is None):
            return True
        prefix = " - ".join(destination.stem.split(" - ")[:2])
        if path.stem == prefix or path.stem.startswith(prefix + " - "):
            return True
    return False


async def plan_audit(scan: ScanResult, config: Config, tmdb, tvmaze=None, identities=None,
                     skip_paths: set[Path] | None = None) -> list[Proposal]:
    proposals: list[Proposal] = []
    for item in scan.items:
        if skip_paths and item.path in skip_paths:
            proposals.append(Proposal(item, "SKIP", "skipped for this plan"))
            continue
        if item.reason:
            proposals.append(Proposal(item, "SKIP", item.reason))
            continue
        if already_canonical(item, config):
            proposals.append(Proposal(item, "SKIP", "already canonical"))
            continue
        if identities is not None and identities.lookup(item)[1]:
            proposals.append(Proposal(item, "SKIP", "permanently skipped"))
            continue
        candidate, confidence, reason, choices, episode_title, canonical_episodes = await resolve(
            item, tmdb, tvmaze if config.providers.tvmaze else None, identities)
        if candidate is None:
            status = "ERROR" if "provider" in reason.lower() or "token" in reason.lower() else "REVIEW"
            proposals.append(Proposal(item, status, reason, alternatives=choices))
            continue
        if candidate.year is None:
            proposals.append(Proposal(item, "REVIEW", "TMDb entity has no canonical year", confidence,
                                      candidate, alternatives=choices))
            continue
        try:
            target, sidecars = destinations(item, candidate, config, episode_title,
                                            canonical_episodes=canonical_episodes)
        except ValueError as error:
            proposals.append(Proposal(item, "REVIEW", str(error), confidence, candidate, alternatives=choices))
            continue
        if target == item.path and all(source == destination for source, destination in sidecars.items()) and not _conflict(
            Proposal(item, "SKIP", "already canonical", destination=target, sidecar_destinations=sidecars)):
            proposals.append(Proposal(item, "SKIP", "already canonical", confidence, candidate, target, sidecars))
            continue
        status = "CONFIRMED" if confidence >= config.matching.auto_apply_threshold else "REVIEW"
        proposals.append(Proposal(item, status, reason, confidence, candidate, target, sidecars, choices))

    occupied: dict[Path, Proposal] = {}
    releases: dict[tuple, Proposal] = {}
    episodes_in_plan: dict[tuple[Path, int, int], Proposal] = {}
    for proposal in proposals:
        if proposal.destination is None or proposal.status == "SKIP":
            continue
        if _conflict(proposal):
            proposal.status = "CONFLICT"
            proposal.reason = "destination exists or another release occupies the target"
        paths = [proposal.destination, *proposal.sidecar_destinations.values()]
        from jellyorganize.naming.movie_versions import release_key as movie_release_key
        release_key = movie_release_key(proposal.destination) if proposal.item.kind == "movie" else (
            proposal.destination.parent, " - ".join(proposal.destination.stem.split(" - ")[:2]))
        if release_key in releases:
            proposal.status = releases[release_key].status = "CONFLICT"
            proposal.reason = releases[release_key].reason = "multiple items resolve to the same release"
        releases[release_key] = proposal
        if proposal.item.kind == "tv" and (target_coverage := coverage(proposal.destination)):
            for number in target_coverage[1]:
                key = (proposal.destination.parent, target_coverage[0], number)
                if key in episodes_in_plan and episodes_in_plan[key] is not proposal:
                    previous = episodes_in_plan[key]
                    proposal.status = previous.status = "CONFLICT"
                    proposal.reason = previous.reason = "multiple items cover the same TV episode"
                episodes_in_plan[key] = proposal
        for path in paths:
            if path in occupied:
                proposal.status = occupied[path].status = "CONFLICT"
                proposal.reason = occupied[path].reason = "multiple items propose the same destination"
            occupied[path] = proposal
    for proposal in proposals:
        if (proposal.status != "CONFIRMED" or proposal.item.kind != "tv" or
                proposal.destination is None):
            continue
        source_code, target_code = coverage(proposal.item.path), coverage(proposal.destination)
        if source_code and target_code and source_code != target_code and any(
            other.path != proposal.item.path and other.path.parent == proposal.item.path.parent and
            overlaps(proposal.item.path, other.path) for other in scan.items
        ):
            proposal.status = "CONFLICT"
            proposal.reason = "another source file covers the same TV episode"
    from jellyorganize.metadata.explanation import explain
    for proposal in proposals:
        proposal.matching = explain(proposal)
    return proposals
