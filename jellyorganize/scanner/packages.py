"""Walk roots without following directory or file symlinks."""

from __future__ import annotations

import time
from pathlib import Path

from guessit import guessit

from jellyorganize.models import MediaItem, ScanResult
from jellyorganize.parsing.guessit_parser import parse_filename
from jellyorganize.parsing.path_hints import path_hints
from jellyorganize.scanner.sidecars import MEDIA_EXTENSIONS, SIDECAR_EXTENSIONS, sidecar_owner


def detect_kind(path: Path, root: Path) -> str | None:
    """Require episode numbering or a year before routing a shared dump item."""
    parsed = guessit(str(path.relative_to(root)))
    if parsed.get("season") is not None and parsed.get("episode") is not None:
        return "tv"
    if path_hints(path, root, "tv").get("folder_season") is not None and parse_filename(path, "tv").get("episode"):
        return "tv"
    if parsed.get("type") == "episode":
        return None
    # A series folder can have a year too. A movie needs a year in its own
    # filename before it is safe to route without an episode code.
    if parse_filename(path, "movie").get("year") is not None:
        return "movie"
    return None


def scan(root: Path, kind: str, *, minimum_age_seconds: int = 0, ignored_extensions: tuple[str, ...] = (),
         ignored_directories: tuple[str, ...] = ()) -> ScanResult:
    result = ScanResult()
    root = root.absolute()
    if root.is_symlink():
        result.skipped.append((root, "unsafe root symlink"))
        return result
    if not root.exists():
        return result
    if not root.is_dir():
        result.skipped.append((root, "unsafe root"))
        return result
    ignored = {extension.lower() for extension in ignored_extensions}
    ignored_folders = {name.casefold() for name in ignored_directories} if kind in ("tv", "auto") else set()
    now = time.time()

    def visit(directory: Path, ignored_tv_context: bool = False) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError as error:
            result.skipped.append((directory, f"unreadable: {error}"))
            return
        files: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                result.skipped.append((entry, "symlink"))
            elif entry.is_dir():
                visit(entry, ignored_tv_context or entry.name.casefold() in ignored_folders)
            elif entry.is_file():
                files.append(entry)
        eligible: list[Path] = []
        for path in files:
            if path.suffix.lower() in ignored:
                result.skipped.append((path, "temporary extension"))
                continue
            try:
                stat = path.stat()
            except OSError as error:
                result.skipped.append((path, f"unreadable: {error}"))
                continue
            if now - stat.st_mtime < minimum_age_seconds:
                result.skipped.append((path, "possibly incomplete"))
            else:
                eligible.append(path)
        media = [path for path in eligible if path.suffix.lower() in MEDIA_EXTENSIONS]
        items = {}
        for path in media:
            detected = detect_kind(path, root) if kind == "auto" else kind
            item_kind = detected or "movie"
            hints = {**parse_filename(path, item_kind), **path_hints(path, root, item_kind)}
            if path.parent != root and "folder_title" not in hints:
                package_hints = parse_filename(Path(path.parent.name), item_kind)
                if package_hints.get("title"):
                    hints["package_title"] = package_hints["title"]
                if package_hints.get("year"):
                    hints["package_year"] = package_hints["year"]
            item = MediaItem(path=path, kind=item_kind, root=root, hints=hints)
            if detected is None and kind == "auto":
                item.reason = "media type is ambiguous; add a movie year or TV season and episode number"
            # Bonus folders can contain both unnumbered featurettes and real
            # S00E## specials. Only the latter are eligible for matching.
            if item_kind == "tv" and ignored_tv_context and not (hints.get("season") == 0 and hints.get("episode")):
                item.reason = "ignored TV extra"
            items[path] = item
        special_in_directory = any(item.reason is None for item in items.values()) if ignored_tv_context else False
        for path in eligible:
            if path in items:
                continue
            if path.suffix.lower() in SIDECAR_EXTENSIONS:
                directory_kind = kind
                if kind == "auto":
                    directory_kind = "tv" if any(item.kind == "tv" for item in items.values()) else "movie"
                owner = sidecar_owner(path, media, directory_kind)
                if owner is None:
                    if ignored_tv_context and not special_in_directory:
                        result.skipped.append((path, "ignored TV extra sidecar"))
                    else:
                        result.unassociated.append(path)
                else:
                    items[owner].sidecars.append(path)
            else:
                result.skipped.append((path, "unsupported file"))
        # A known sidecar that is too new or cannot be associated blocks the
        # containing package from later eligibility. It must not be stranded.
        unsafe_sidecar = any(path.parent == directory and (
            (reason == "possibly incomplete" and path.suffix.lower() in SIDECAR_EXTENSIONS)
            or reason == "temporary extension") for path, reason in result.skipped)
        unsafe_sidecar = unsafe_sidecar or any(path.parent == directory for path in result.unassociated)
        if unsafe_sidecar:
            for item in items.values():
                if item.reason is None:
                    item.reason = "package has temporary, incomplete, or unassociated files"
        result.items.extend(items.values())

    visit(root)
    result.items.sort(key=lambda item: str(item.path))
    return result
