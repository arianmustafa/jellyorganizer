"""In-memory proposals for media in Incoming; no filesystem changes."""

from __future__ import annotations

from jellyorganize.config import Config
from jellyorganize.metadata.matcher import resolve
from jellyorganize.models import Proposal, ScanResult
from jellyorganize.naming.jellyfin import destinations
from jellyorganize.parsing.episode_codes import coverage, overlaps
from jellyorganize.scanner.sidecars import MEDIA_EXTENSIONS


async def plan_ingest(scan: ScanResult, config: Config, tmdb, tvmaze=None, identities=None, skip_paths=None) -> list[Proposal]:
    proposals: list[Proposal] = []
    skip_paths = skip_paths or set()
    from jellyorganize.filesystem.links import LinkedImports
    from jellyorganize.filesystem.safety import UnsafePath
    from jellyorganize.filesystem.apply import _release_conflict
    from jellyorganize.planning.store import entry_from_proposal
    links = LinkedImports(config)
    repairs = set()
    for item in scan.items:
        if item.path in skip_paths:
            proposals.append(Proposal(item, "SKIP", "skipped for this plan"))
            continue
        if identities is not None and identities.lookup(item)[1]:
            proposals.append(Proposal(item, "SKIP", "permanently skipped"))
            continue
        if item.reason:
            status = ("ERROR" if item.reason.startswith("completion check failed:") else
                      "REVIEW" if item.reason.startswith("media type is ambiguous") else "SKIP")
            proposals.append(Proposal(item, status, item.reason))
            continue
        try:
            linked = links.package(item)
        except (OSError, UnsafePath) as error:
            proposals.append(Proposal(item, "CONFLICT", f"retained hard-link import changed: {error}"))
            continue
        if linked is not None:
            entry, targets, complete = linked
            if complete:
                proposals.append(Proposal(item, "SKIP", "already hard-linked; originals retained"))
            elif config.filesystem.mode != "hardlink":
                proposals.append(Proposal(item, "CONFLICT", "tracked hard-link destination missing; enable hardlink mode to repair"))
            else:
                proposals.append(Proposal(item, "CONFIRMED", "repair missing links using saved identity", entry.confidence,
                                          entry.candidate, targets[item.path],
                                          {source: target for source, target in targets.items() if source != item.path}))
                repairs.add(item.path)
            continue
        candidate, confidence, reason, choices, episode_title, canonical_episodes = await resolve(
            item, tmdb, tvmaze if config.providers.tvmaze else None, identities,
            confirm_exact_movies=config.matching.confirm_exact_movies)
        if candidate is None:
            status = "ERROR" if "provider" in reason.lower() or "token" in reason.lower() else "REVIEW"
            proposals.append(Proposal(item, status, reason, alternatives=choices))
            continue
        if candidate.year is None:
            proposals.append(Proposal(item, "REVIEW", "TMDb entity has no canonical year", confidence, candidate,
                                      alternatives=choices))
            continue
        try:
            target, sidecars = destinations(item, candidate, config, episode_title,
                                            canonical_episodes=canonical_episodes)
        except ValueError as error:
            proposals.append(Proposal(item, "REVIEW", str(error), confidence, candidate, alternatives=choices))
            continue
        status = "CONFIRMED" if confidence >= config.matching.auto_apply_threshold else "REVIEW"
        proposals.append(Proposal(item, status, reason, confidence, candidate, target, sidecars, choices))
    occupied: dict = {}
    releases: dict = {}
    episodes_in_plan: dict = {}
    for proposal in proposals:
        if proposal.destination is None:
            continue
        owned = set()
        if proposal.item.path in repairs:
            entry = entry_from_proposal(proposal, config)
            owned = {file.destination for file in entry.files if links.owned(file, entry.source_root, entry.destination_root)}
        paths = [proposal.destination, *proposal.sidecar_destinations.values()]
        release_key = (proposal.destination.parent if proposal.item.kind == "movie"
                       else (proposal.destination.parent, " - ".join(proposal.destination.stem.split(" - ")[:2])))
        if proposal.item.kind == "movie":
            duplicate = proposal.destination.parent.is_dir() and any(
                path.suffix.lower() in MEDIA_EXTENSIONS for path in proposal.destination.parent.iterdir() if path.is_file())
        else:
            prefix = release_key[1]
            duplicate = proposal.destination.parent.is_dir() and any(
                path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and path.stem.startswith(prefix)
                for path in proposal.destination.parent.iterdir())
            target_coverage = coverage(proposal.destination)
            duplicate = duplicate or (proposal.destination.parent.is_dir() and any(
                path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and
                (overlaps(proposal.destination, path) or
                 (target_coverage and len(target_coverage[1]) > 1 and coverage(path) is None))
                for path in proposal.destination.parent.iterdir()))
        if proposal.item.path in repairs:
            duplicate = _release_conflict(entry, owned)
        if len(paths) != len(set(paths)) or any(path.exists() for path in paths if path not in owned) or duplicate:
            proposal.status = "CONFLICT"
            proposal.reason = "destination exists, duplicate media, or sidecar names collide"
        if release_key in releases:
            proposal.status = "CONFLICT"
            proposal.reason = "multiple items resolve to the same release"
            releases[release_key].status = "CONFLICT"
            releases[release_key].reason = "multiple items resolve to the same release"
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
                proposal.status = "CONFLICT"
                proposal.reason = "multiple items propose the same destination"
                occupied[path].status = "CONFLICT"
                occupied[path].reason = "multiple items propose the same destination"
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
    return proposals
