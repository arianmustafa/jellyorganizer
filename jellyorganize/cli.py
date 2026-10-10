"""CLI: planning is read-only; explicit apply and opt-in auto use one executor."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
import json
from pathlib import Path

import httpx
from pydantic import ValidationError

from jellyorganize import __version__
from jellyorganize.config import Config, load_config, config_path, init_config
from jellyorganize.completion import CompletionTracker
from jellyorganize.operations import Operations
from jellyorganize.service import token, install as install_service, units as service_units
from jellyorganize.filesystem.recovery import recover
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.filesystem.undo import undo_transaction
from jellyorganize.identity.store import IdentityStore
from jellyorganize.metadata.cache import MetadataCache
from jellyorganize.metadata.base import ProviderError
from jellyorganize.metadata.tmdb import TMDbProvider
from jellyorganize.metadata.tvmaze import TVmazeProvider
from jellyorganize.planning.audit import plan_audit
from jellyorganize.planning.ingest import plan_ingest
from jellyorganize.planning.store import PlanStore
from jellyorganize.models import MediaItem, Proposal
from jellyorganize.scanner.incoming import scan_incoming
from jellyorganize.scanner.library import scan_library


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="jellyorganize", description="Automatic Incoming organizer and Jellyfin library repair tool")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument("--config", type=Path, help="TOML configuration path")
    root.add_argument("-v", action="count", default=0, help="show additional planning candidates")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="inspect files without network access")
    scan.add_argument("location", choices=["incoming", "library"])
    scan.add_argument("kind", choices=["movies", "tv", "all"])
    organize = commands.add_parser("organize", help="automatically organize Incoming; leave uncertain files untouched")
    organize.add_argument("kind", choices=["movies", "tv", "all"], nargs="?", default="all")
    organize.add_argument("--dry-run", action="store_true", help="save and show decisions without moving or linking files")
    run = commands.add_parser("run", help="scheduled Incoming run with compact logs and persistent exceptions")
    run.add_argument("kind", choices=["movies", "tv", "all"], nargs="?", default="all")
    configuration = commands.add_parser("config", help="create, inspect, or validate TOML configuration")
    configuration.add_argument("action", choices=["init", "show", "check"])
    configuration.add_argument('--download-client', action='store_true', help='also authenticate and test the configured qBittorrent API')
    service = commands.add_parser("service", help="generate or install a user systemd timer")
    service.add_argument("action", choices=["install", "show"])
    service.add_argument("--directory", type=Path, help="unit output directory (default: ~/.config/systemd/user)")
    service.add_argument("--save-credential", action="store_true", help="store the current token privately for scheduled jobs")
    ready = commands.add_parser("ready", help="acknowledge a completed file or download folder in Incoming")
    ready.add_argument("path", type=Path)
    commands.add_parser("recover", help="resume interrupted transactions using saved decisions")
    doctor = commands.add_parser("doctor", help="check root access and actual hard-link support using temporary probes")
    doctor.add_argument("--json", action="store_true")
    for command in ("status", "exceptions"):
        display = commands.add_parser(command, help="show unattended run health" if command == "status" else "show unresolved files without repeated reports")
        display.add_argument("--json", action="store_true")
    benchmark = commands.add_parser("benchmark", help="measure matching against labeled filename examples")
    benchmark.add_argument("--corpus", type=Path, help="custom labeled corpus JSON")
    benchmark.add_argument("--live", action="store_true", help="use live providers instead of frozen snapshots")
    benchmark.add_argument("--output", type=Path, help="save the full machine-readable report")
    download = commands.add_parser("import-download", help="import a completed torrent; hard-link mode allows continued seeding")
    download.add_argument("--torrent", required=True, help="qBittorrent info hash (Qui: {hash})")
    download.add_argument("--dry-run", action="store_true", help="verify client and files and save a handoff plan without changing media")
    download.add_argument("--handoff-only", action="store_true", help="import into Incoming without metadata organization")
    credentials = commands.add_parser("credentials", help="save a private credential for manual runs and download hooks")
    credentials.add_argument("provider", choices=["tmdb", "qbittorrent", "qbittorrent-proxy", "jellyfin", "webhook"])
    credentials.add_argument("--from-env", action="store_true", help="save the provider's current environment credential without prompting")
    ingest = commands.add_parser("ingest", help="save a dry-run plan; --auto applies only strong matches")
    ingest.add_argument("kind", choices=["movies", "tv", "all"])
    ingest.add_argument("--auto", action="store_true", help="apply only high-confidence CONFIRMED items")
    audit = commands.add_parser("audit", help="save a repair plan; --auto applies only strong matches")
    audit.add_argument("kind", choices=["movies", "tv", "all"])
    audit.add_argument("--auto", action="store_true", help="apply only high-confidence CONFIRMED repairs")
    review = commands.add_parser("review", help="show attention items and record choices in a new plan")
    review.add_argument("plan_id")
    explanation = commands.add_parser("explain", help="show the matching evidence saved in a plan")
    explanation.add_argument("plan_id")
    explanation.add_argument("--json", action="store_true")
    integrations = commands.add_parser("integrations", help="retry pending webhooks and Jellyfin refreshes")
    integrations.add_argument("--json", action="store_true")
    identify = commands.add_parser("identify", help="persist a manually verified TMDb identity")
    identify.add_argument("path", type=Path)
    identify.add_argument("--tmdb", required=True, type=int)
    apply = commands.add_parser("apply", help="validate and execute CONFIRMED items from a saved plan")
    apply.add_argument("plan_id")
    undo = commands.add_parser("undo", help="restore unchanged files from an apply transaction")
    undo.add_argument("transaction_id")
    cache = commands.add_parser("cache", help="inspect or clear metadata cache")
    cache.add_argument("action", choices=["stats", "clear"])
    return root


def kinds(selection: str) -> tuple[str, ...]:
    return ("movie", "tv") if selection == "all" else ("movie",) if selection == "movies" else ("tv",)


def show_scan(result, verbose: int) -> None:
    for item in result.items:
        label = "UNKNOWN" if item.reason and item.reason.startswith("media type is ambiguous") else item.kind.upper()
        print(f"[MEDIA {label}] {item.path}")
        if item.reason:
            print(f"  SKIP: {item.reason}")
        if item.path.parent != item.root:
            print(f"  package: {item.path.parent}")
        for sidecar in item.sidecars:
            print(f"  sidecar: {sidecar}")
        path_keys = {"tmdb_id", "folder_title", "folder_year", "folder_season", "package_title", "package_year"}
        print(f"  GuessIt: { {key: value for key, value in item.hints.items() if key not in path_keys} }")
        print(f"  path hints: { {key: value for key, value in item.hints.items() if key in path_keys} }")
    for path in result.unassociated:
        print(f"[UNASSOCIATED] {path}")
    for path, reason in result.skipped:
        print(f"[SKIP {reason}] {path}")
    print(f"Media: {len(result.items)}  Unassociated: {len(result.unassociated)}  Skipped: {len(result.skipped)}")


def review_source_current(entry, item: MediaItem) -> bool:
    expected = {state.source: state for state in entry.source_states}
    if not expected or set(expected) != {item.path, *item.sidecars}:
        return False
    try:
        for path, state in expected.items():
            current = path.lstat()
            if path.is_symlink() or not path.is_file():
                return False
            if (current.st_size, current.st_mtime_ns, current.st_ino, current.st_dev) != (
                state.size, state.mtime_ns, state.inode, state.device
            ):
                return False
    except OSError:
        return False
    return True


def show_plan(plan, verbose: int, *, auto: bool = False) -> int:
    counts = {key: 0 for key in ("CONFIRMED", "REVIEW", "SKIP", "CONFLICT", "ERROR")}
    print(f"Plan: {plan.plan_id}")
    print(f"Workflow: {plan.workflow.upper()}")
    print(f"Transfer: {plan.transfer_mode.upper()}" + (" (originals retained)" if plan.transfer_mode == "hardlink" else ""))
    for entry in plan.entries:
        counts[entry.status] += 1
        label = "UNKNOWN" if entry.reason.startswith("media type is ambiguous") else entry.kind.upper()
        print(f"[{entry.status}] {label} {entry.source}")
        print(f"  {entry.reason}")
        if verbose or entry.status == "REVIEW":
            from jellyorganize.metadata.explanation import lines
            for line in lines(entry.matching):
                print(f"  {line}")
        if entry.candidate:
            print(f"  TMDb: {entry.candidate.title} ({entry.candidate.year or '?'}) [tmdbid-{entry.candidate.provider_id}]")
            print(f"  Confidence: {entry.confidence:.2f}")
        if entry.destination:
            print(f"  -> {entry.destination}")
        for file in entry.files[1:]:
            print(f"  sidecar: {file.source} -> {file.destination}")
        if verbose and entry.alternatives:
            print("  candidates: " + ", ".join(f"{row.title} ({row.year}) [{row.provider}:{row.provider_id}]" for row in entry.alternatives))
    print("  ".join(f"{key}: {value}" for key, value in counts.items()))
    if counts["REVIEW"] or counts["CONFLICT"] or counts["ERROR"]:
        print(f"Attention: unresolved files stay in place. Details: jellyorganize review {plan.plan_id}")
    if not auto:
        print(f"Apply:  jellyorganize apply {plan.plan_id}")
    if counts["CONFLICT"]:
        return 3
    if counts["REVIEW"] or counts["ERROR"]:
        return 2
    return 0


async def make_plan(config: Config, workflow: str, selection: str, verbose: int,
                    skip_paths: set[Path] | None = None, *, auto: bool = False, quiet: bool = False,
                    wait_for_completion: bool = False) -> int:
    recovery_counts = {"RECOVERED": 0}
    if auto:
        recovery_counts, errors = recover(config)
        if errors:
            raise ValueError("unfinished transactions need attention: " + "; ".join(errors))
        if recovery_counts["RECOVERED"] and not quiet:
            print(f"Recovered interrupted items: {recovery_counts['RECOVERED']}")
    if wait_for_completion and workflow == "ingest":
        fast = config.model_copy(deep=True)
        fast.incoming.minimum_age_seconds = 0
        tracker = CompletionTracker(config.state_dir / "completion.sqlite3")
        delay = 0
        for kind in kinds(selection):
            for item in scan_incoming(fast, kind).items:
                if not item.reason:
                    delay = max(delay, tracker.wait_seconds(item, config.incoming))
        if delay > 0:
            print(f"Checking download completion; waiting up to {delay:.0f} seconds, then rescanning Incoming",
                  file=sys.stderr if quiet else sys.stdout)
            await asyncio.sleep(delay)
    cache = MetadataCache(config.cache_path)
    identities = IdentityStore(config.identity_path)
    store = PlanStore(config.state_dir / "plans")
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        tmdb = TMDbProvider(token(config), cache, client)
        tvmaze = TVmazeProvider(cache, client) if config.providers.tvmaze else None
        all_proposals = []
        selected_kinds = kinds(selection)
        for kind in selected_kinds:
            scan = (scan_incoming(config, kind, include_unassigned=kind == selected_kinds[0])
                    if workflow == "ingest" else scan_library(config, kind))
            if workflow == "ingest":
                scan = CompletionTracker(config.state_dir / "completion.sqlite3").filter(scan, config.incoming)
            planner = plan_ingest if workflow == "ingest" else plan_audit
            proposals = await planner(scan, config, tmdb, tvmaze, identities, skip_paths)
            if auto:
                threshold = max(0.97, config.matching.auto_apply_threshold)
                for proposal in proposals:
                    if proposal.status == "CONFIRMED" and (proposal.confidence < threshold or
                       proposal.candidate is None or proposal.candidate.provider != "tmdb"):
                        proposal.status = "REVIEW"
                        proposal.reason = "below automatic safety threshold"
            all_proposals.extend(proposals)
            paths = config.movies if kind == "movie" else config.tv
            root = config.incoming_root(kind) if workflow == "ingest" else paths.library
            if not root.exists():
                label = "Incoming" if workflow == "ingest" else "library"
                all_proposals.append(Proposal(MediaItem(path=root, kind=kind, root=root), "ERROR", f"{label} root is missing"))
            for path, reason in scan.skipped:
                status = "ERROR" if path == root or reason.startswith("unreadable:") else "SKIP"
                all_proposals.append(Proposal(MediaItem(path=path, kind=kind, root=root), status, reason))
            for path in scan.unassociated:
                all_proposals.append(Proposal(MediaItem(path=path, kind=kind, root=root), "REVIEW", "unassociated sidecar"))
        plan = store.create(all_proposals, config, kinds(selection), workflow=workflow)
    if quiet:
        plan_status = (3 if any(entry.status == "CONFLICT" for entry in plan.entries) else
                       2 if any(entry.status in {"REVIEW", "ERROR"} for entry in plan.entries) else 0)
    else:
        plan_status = show_plan(plan, verbose, auto=auto)
    if not auto:
        return plan_status
    threshold = max(0.97, config.matching.auto_apply_threshold)
    # Apply exactly the saved decisions. No provider lookup occurs after this load.
    saved = store.load(plan.plan_id)
    eligible = any(entry.status == "CONFIRMED" and entry.candidate is not None and
                   entry.candidate.provider == "tmdb" and entry.confidence >= threshold for entry in saved.entries)
    if not eligible:
        if not quiet:
            print("Auto: no eligible items; media unchanged")
        status = 1 if any(entry.status == "ERROR" for entry in saved.entries) else plan_status
        summary = await finish_run(config, saved, status, recovered_items=recovery_counts["RECOVERED"])
        if quiet:
            print(json.dumps(summary, sort_keys=True))
        return status
    transaction, counts = apply_plan(saved, config, config.state_dir / "transactions", auto_threshold=threshold)
    if not quiet:
        show_transaction(transaction, counts)
    status = apply_status(saved, counts)
    summary = await finish_run(config, saved, status, counts, transaction, recovered_items=recovery_counts["RECOVERED"])
    if quiet:
        print(json.dumps(summary, sort_keys=True))
    return status


async def deliver_integrations(config):
    if not (config.notifications.enabled or config.jellyfin.enabled):
        return None
    from jellyorganize.integrations import flush
    result = await flush(config)
    for error in result["errors"]:
        print(f"Integration pending retry: {error}", file=sys.stderr)
    return result


async def finish_run(config, plan, status, counts=None, transaction=None, *, recovered_items=0):
    summary = Operations(config.state_dir / "operations.sqlite3").record(
        plan, status, counts, transaction, recovered_items=recovered_items, config=config)
    result = await deliver_integrations(config)
    if result is not None:
        summary["integrations"] = result
    return summary


def show_transaction(transaction, counts: dict[str, int]) -> None:
    print(f"Transaction: {transaction.data['transaction_id']}")
    for item in transaction.data["items"]:
        print(f"[{item['status'].upper()}] {item['source']}")
        if item.get("error"):
            print(f"  {item['error']}")
        if item.get("destination_files_present"):
            print("  Inspect destination files: " + ", ".join(item["destination_files_present"]))
    print("  ".join(f"{key}: {value}" for key, value in counts.items()))
    if counts.get("APPLIED", 0):
        print(f"Undo: jellyorganize undo {transaction.data['transaction_id']}")


def apply_status(plan, counts: dict[str, int]) -> int:
    if counts["ERROR"] or counts["STALE"] or any(entry.status == "ERROR" for entry in plan.entries):
        return 1
    if counts["CONFLICT"] or any(entry.status == "CONFLICT" for entry in plan.entries):
        return 3
    return 2 if any(entry.status == "REVIEW" for entry in plan.entries) else 0


async def ingest(config: Config, selection: str, verbose: int, skip_paths: set[Path] | None = None) -> int:
    return await make_plan(config, "ingest", selection, verbose, skip_paths)


async def audit(config: Config, selection: str, verbose: int, skip_paths: set[Path] | None = None) -> int:
    return await make_plan(config, "audit", selection, verbose, skip_paths)


async def identify(config: Config, path: Path, tmdb_id: int) -> int:
    if tmdb_id <= 0:
        print("TMDb ID must be positive", file=sys.stderr)
        return 4
    path = path.expanduser().absolute()
    item = next((item for scanner in (scan_incoming, scan_library) for kind in ("movie", "tv")
                 for item in scanner(config, kind).items if item.path == path), None)
    if item is None or item.reason:
        print("Path is not eligible media in a configured Incoming or Library root", file=sys.stderr)
        return 4
    async with httpx.AsyncClient(timeout=10.0) as client:
        tmdb = TMDbProvider(token(config), MetadataCache(config.cache_path), client)
        try:
            candidate = await (tmdb.get_movie(str(tmdb_id)) if item.kind == "movie" else tmdb.get_series(str(tmdb_id)))
        except ProviderError as error:
            print(f"Identification failed: {error}", file=sys.stderr)
            return 5
    IdentityStore(config.identity_path).confirm(item, candidate, "identify command")
    print(f"Saved identity: {item.path} -> {candidate.title} ({candidate.year or '?'}) [tmdbid-{candidate.provider_id}]")
    print("Run ingest or audit again to create a new immutable plan.")
    return 0


async def review(config: Config, plan_id: str) -> int:
    plan = PlanStore(config.state_dir / "plans").load(plan_id)
    scanner = scan_incoming if plan.workflow == "ingest" else scan_library
    attention = [entry for entry in plan.entries if entry.status in ("REVIEW", "CONFLICT", "ERROR")]
    if not attention:
        print("No items require review.")
        return 0
    identities = IdentityStore(config.identity_path)
    changed = False
    skip_once: set[Path] = set()
    for entry in attention:
        print(f"[{entry.status}] {entry.source}\n  {entry.reason}")
        from jellyorganize.metadata.explanation import lines
        for line in lines(entry.matching):
            print(f"  {line}")
        if entry.reason.startswith("media type is ambiguous"):
            print("  Add a movie year or TV season and episode number to the source name, then run ingest all again.")
            continue
        if entry.reason == "unassociated sidecar":
            print(f"  Correct the source or sidecar association, then run {plan.workflow} again.")
            continue
        candidates = [row for row in entry.alternatives if row.provider == "tmdb" and row.kind == entry.kind]
        if entry.status != "REVIEW":
            continue
        for number, candidate in enumerate(candidates, 1):
            print(f"  {number}. {candidate.title} ({candidate.year or '?'}) [tmdbid-{candidate.provider_id}]")
        print("  m. Enter TMDb ID   s. Skip this plan   p. Skip permanently   Enter. Leave unresolved")
        if not sys.stdin.isatty():
            print(f"  Use: jellyorganize identify {shlex.quote(str(entry.source))} --tmdb ID")
            continue
        try:
            answer = input("Choice: ").strip().lower()
        except EOFError:
            print("  Review ended without a choice.")
            break
        item = next((item for item in scanner(config, entry.kind).items if item.path == entry.source), None)
        if item is None or item.reason or not review_source_current(entry, item):
            print(f"  Source or sidecars changed; run {plan.workflow} again.")
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            identities.confirm(item, candidates[int(answer) - 1], "review choice")
            changed = True
        elif answer == "m":
            try:
                tmdb_id = int(input("TMDb ID: ").strip())
            except (ValueError, EOFError):
                print("  Invalid ID")
                continue
            if await identify(config, entry.source, tmdb_id) == 0:
                changed = True
        elif answer == "p":
            identities.skip_permanently(item)
            changed = True
        elif answer == "s":
            skip_once.add(item.path)
            changed = True
    if changed:
        print("Choices saved. Creating a new plan; the original plan remains unchanged.")
        selection = "all" if set(plan.scope) == {"movie", "tv"} else "movies" if plan.scope == ["movie"] else "tv"
        return await make_plan(config, plan.workflow, selection, 0, skip_once)
    if any(entry.status == "CONFLICT" for entry in attention):
        return 3
    return 2 if any(entry.status == "REVIEW" for entry in attention) else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "config" and args.action == "init":
        try:
            path = init_config(args.config)
            print(f"Created: {path}\nEdit the paths, then run: jellyorganize --config {shlex.quote(str(path))} config check")
            return 0
        except (OSError, ValueError) as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 4
    try:
        config = load_config(args.config)
    except (OSError, ValueError, ValidationError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 4
    if args.command == "doctor":
        from jellyorganize.filesystem.doctor import check
        report = check(config)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Filesystem mode: {report['mode']}")
            for row in report["checks"]:
                print(f"[{row['status']}] {row['stage']}: {row['source']} → {row['destination']}")
                print("  " + (row.get("error") or ("Hard links supported" if row["link_supported"] else
                                                    "Writable destination; move mode will use verified copying")))
        return 0 if report["passed"] else 4
    if args.command == "config":
        if args.action == "show":
            print(json.dumps(config.model_dump(mode="json"), indent=2))
            return 0
        problems = []
        roots = {config.incoming_root(kind) for kind in ("movie", "tv")} | {config.movies.library, config.tv.library}
        if config.downloads.path:
            roots.add(config.downloads.path)
            if not config.incoming.path or not config.qbittorrent.url:
                problems.append('download integration requires incoming.path and qbittorrent.url')
        for root in sorted(roots):
            if root.is_symlink() or not root.is_dir():
                problems.append(f"missing or symlinked media directory: {root}")
            elif not os.access(root, os.R_OK | os.W_OK | os.X_OK):
                problems.append(f"media directory is not readable and writable: {root}")
        try:
            configured_token = bool(token(config))
        except (OSError, ValueError) as error:
            problems.append(str(error))
            configured_token = False
        print(f"Configuration: {(args.config or config_path()).expanduser().absolute()}")
        print(f"Import transfer mode: {config.filesystem.mode}" + (" (originals retained)" if config.filesystem.mode == "hardlink" else ""))
        print(f"TMDb credential available: {'yes' if configured_token else 'no (cached lookups only)'}")
        if config.notifications.enabled or config.jellyfin.enabled:
            from jellyorganize.integrations import secret, webhook_url
            for enabled, label, read in (
                (config.notifications.enabled, "Webhook", lambda: webhook_url(config)),
                (config.jellyfin.enabled, "Jellyfin", lambda: secret(config.jellyfin.api_key_file, "JELLYFIN_API_KEY")),
            ):
                if enabled:
                    try:
                        available = bool(read())
                        print(f"{label} credential available: {'yes' if available else 'no'}")
                    except (OSError, ValueError) as error:
                        problems.append(f"{label} credential unavailable: {type(error).__name__}")
        if config.qbittorrent.url:
            from jellyorganize.downloads.qbittorrent import private_password
            print(f"qBittorrent API: {config.qbittorrent.url}")
            for username, path, environment, label in (
                (config.qbittorrent.username, config.qbittorrent.password_file, 'QBITTORRENT_PASSWORD', 'qBittorrent'),
                (config.qbittorrent.basic_username, config.qbittorrent.basic_password_file, 'QBITTORRENT_BASIC_PASSWORD', 'Reverse proxy'),
            ):
                if username:
                    try:
                        available = bool(private_password(path, environment))
                        print(f"{label} credential available: {'yes' if available else 'no'}")
                        if not available:
                            problems.append(f'{label} credential is missing')
                    except (OSError, ValueError) as error:
                        problems.append(str(error))
        if args.download_client:
            from jellyorganize.downloads.qbittorrent import QBittorrentClient
            try:
                client = QBittorrentClient(config.qbittorrent)
                with client.session() as session:
                    response = client.request(session, 'GET', 'app/version')
                    version = response.text.strip()
                    if len(version) > 64 or not version or not all(character.isalnum() or character in '.-+' for character in version):
                        raise ValueError('qBittorrent returned an invalid version response')
                    print(f'qBittorrent connection: OK ({version})')
            except (OSError, ValueError) as error:
                problems.append(str(error))
        for problem in problems:
            print(problem, file=sys.stderr)
        return 4 if problems else 0
    if args.command == "benchmark":
        from jellyorganize.benchmark import evaluate
        try:
            result = asyncio.run(evaluate(config, args.corpus, live=args.live))
            if args.output:
                args.output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps({key: value for key, value in result.items() if key != "results"}, indent=2))
            for row in result["results"]:
                if (row["accepted"] and not row["correct"]) or row["accepted"] != row["expected_auto"]:
                    print(f"[FAILED] {row['name']}: {row['reason']}")
            return 0 if result["passed"] else 1
        except (OSError, ValueError) as error:
            print(f"Benchmark error: {error}", file=sys.stderr)
            return 1
    if args.command == "service":
        try:
            if args.action == "show":
                for name, content in service_units(config, args.config).items():
                    print(f"# {name}\n{content}")
            else:
                directory = install_service(config, args.config, args.directory, save_token=args.save_credential)
                print(f"Installed units: {directory}\nEnable on this host with:\nsystemctl --user daemon-reload\nsystemctl --user enable --now jellyorganize.timer")
            return 0
        except (OSError, ValueError) as error:
            print(f"Service configuration error: {error}", file=sys.stderr)
            return 4
    if args.command in {"status", "exceptions"}:
        operations = Operations(config.state_dir / "operations.sqlite3")
        data = operations.status() if args.command == "status" else operations.exceptions()
        if args.json:
            print(json.dumps(data, indent=2))
        elif args.command == "status":
            print(f"Last run: {data['last_run']['finished_at'] if data['last_run'] else 'never'}")
            print(f"Last exit code: {data['last_run']['exit_code'] if data['last_run'] else '-'}")
            print(f"Open exceptions: {data['open_exceptions']}")
            print(f"Pending integrations: {data['pending_integrations']}")
        else:
            for item in data:
                print(f"[{item['status']}] {item['source']}\n  {item['reason']}")
            print(f"Open exceptions: {len(data)}")
        return 0
    if args.command == "integrations":
        from jellyorganize.integrations import flush
        result = asyncio.run(flush(config))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Sent: {result['sent']}  Failed: {result['failed']}  Pending: {result['pending']}")
            for error in result["errors"]:
                print(error, file=sys.stderr)
        return 1 if result["failed"] else 0
    if args.command == "recover":
        try:
            counts, errors = recover(config)
            print("  ".join(f"{name}: {count}" for name, count in counts.items()))
            for error in errors:
                print(error, file=sys.stderr)
            asyncio.run(deliver_integrations(config))
            return 1 if errors else 0
        except (OSError, ValueError) as error:
            print(f"Recovery error: {error}", file=sys.stderr)
            return 1
    if args.command == "ready":
        path = args.path.expanduser().absolute()
        fast = config.model_copy(deep=True)
        fast.incoming.minimum_age_seconds = 0
        items = [item for kind in ("movie", "tv") for item in scan_incoming(fast, kind, include_unassigned=kind == "movie").items
                 if item.path == path or path in item.path.parents]
        if not items or any(item.reason for item in items):
            print("Path must identify eligible completed media in Incoming; remove temporary or unassociated files first", file=sys.stderr)
            return 4
        try:
            tracker = CompletionTracker(config.state_dir / "completion.sqlite3")
            for item in items:
                tracker.acknowledge(item)
            print(f"Acknowledged completed files: {len(items)}")
            return 0
        except (OSError, ValueError) as error:
            print(f"Completion acknowledgement error: {error}", file=sys.stderr)
            return 1
    if args.command == "scan":
        selected_kinds = kinds(args.kind)
        for kind in selected_kinds:
            print(f"{args.location.upper()} {kind.upper()}")
            result = (scan_incoming(config, kind, include_unassigned=kind == selected_kinds[0])
                      if args.location == "incoming" else scan_library(config, kind))
            show_scan(result, args.v)
        return 0
    if args.command in ("run", "organize", "ingest", "audit"):
        try:
            workflow = "ingest" if args.command in {"organize", "run"} else args.command
            auto = True if args.command == "run" else not args.dry_run if args.command == "organize" else args.auto
            return asyncio.run(make_plan(config, workflow, args.kind, args.v, auto=auto, quiet=args.command == "run",
                                         wait_for_completion=args.command == "organize" and auto))
        except (OSError, ValueError, ValidationError) as error:
            if auto:
                Operations(config.state_dir / "operations.sqlite3").failure(str(error), config=config)
                asyncio.run(deliver_integrations(config))
            print(f"Application error: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted; unfinished moves will be checked on the next run", file=sys.stderr)
            return 130
    if args.command == "import-download":
        from jellyorganize.downloads.handoff import import_torrent
        try:
            plan, result = import_torrent(config, args.torrent, dry_run=args.dry_run)
            print(json.dumps({"download": result, "plan_id": plan.plan_id if plan else None,
                              "transfer_mode": plan.transfer_mode if plan else config.filesystem.mode}, sort_keys=True))
            if plan is not None and not args.dry_run and not args.handoff_only:
                return asyncio.run(make_plan(config, "ingest", "all", args.v, auto=True, quiet=True, wait_for_completion=True))
            return 0
        except (OSError, ValueError) as error:
            if not args.dry_run:
                Operations(config.state_dir / "operations.sqlite3").failure(str(error), config=config)
                asyncio.run(deliver_integrations(config))
            print(f"Download handoff error: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted; unfinished moves will be checked on the next run", file=sys.stderr)
            return 130
    if args.command == "credentials":
        from jellyorganize.credentials import write_private
        import getpass
        path, environment = {
            "tmdb": (config.service.credential_file, "TMDB_API_TOKEN"),
            "qbittorrent": (config.qbittorrent.password_file, "QBITTORRENT_PASSWORD"),
            "qbittorrent-proxy": (config.qbittorrent.basic_password_file, "QBITTORRENT_BASIC_PASSWORD"),
            "jellyfin": (config.jellyfin.api_key_file, "JELLYFIN_API_KEY"),
            "webhook": (config.notifications.webhook_url_file, "JELLYORGANIZE_WEBHOOK_URL"),
        }[args.provider]
        try:
            if args.from_env:
                value = os.environ.get(environment, "")
                if not value:
                    raise ValueError(f"{environment} is missing")
            else:
                if not sys.stdin.isatty():
                    raise ValueError("use an interactive terminal to enter a credential, or --from-env")
                value = getpass.getpass(f"{args.provider} credential (hidden): ")
            write_private(path, value)
            print(f"Saved private credential: {path}")
            return 0
        except (OSError, ValueError, EOFError) as error:
            print(f"Credential configuration error: {error}", file=sys.stderr)
            return 4
    if args.command == "undo":
        try:
            transaction, counts = undo_transaction(args.transaction_id, config)
        except (OSError, ValueError, KeyError, ValidationError) as error:
            print(f"Cannot undo transaction: {error}", file=sys.stderr)
            return 1
        show_transaction(transaction, counts)
        if counts["ERROR"] or counts["STALE"]:
            return 1
        return 3 if counts["CONFLICT"] else 0
    if args.command == "identify":
        return asyncio.run(identify(config, args.path, args.tmdb))
    if args.command == "explain":
        try:
            plan = PlanStore(config.state_dir / "plans").load(args.plan_id)
            if args.json:
                print(json.dumps([{"source": str(entry.source), "status": entry.status,
                                  "reason": entry.reason, "matching": entry.matching}
                                 for entry in plan.entries], indent=2))
            else:
                from jellyorganize.metadata.explanation import lines
                for entry in plan.entries:
                    print(f"[{entry.status}] {entry.source}\n  {entry.reason}")
                    for line in lines(entry.matching):
                        print(f"  {line}")
                    if not entry.matching:
                        print("  This older plan has no saved matching evidence; create a new ingest or audit plan.")
            return 0
        except (OSError, ValueError) as error:
            print(f"Cannot explain plan: {error}", file=sys.stderr)
            return 1
    if args.command == "review":
        try:
            return asyncio.run(review(config, args.plan_id))
        except (OSError, ValueError, ValidationError) as error:
            print(f"Cannot review plan: {error}", file=sys.stderr)
            return 1
    if args.command == "apply":
        try:
            plan = PlanStore(config.state_dir / "plans").load(args.plan_id)
            transaction, counts = apply_plan(plan, config, config.state_dir / "transactions")
        except (OSError, ValueError, ValidationError) as error:
            print(f"Cannot apply plan: {error}", file=sys.stderr)
            return 1
        show_transaction(transaction, counts)
        status = apply_status(plan, counts)
        asyncio.run(finish_run(config, plan, status, counts, transaction))
        return status
    cache = MetadataCache(config.cache_path)
    if args.action == "stats":
        total, valid = cache.stats()
        print(f"Cache: {total} entries, {valid} valid")
    else:
        print(f"Cleared {cache.clear()} cache entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
