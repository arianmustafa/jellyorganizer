from pathlib import Path

from jellyorganize.config import Config
from jellyorganize.models import ScanResult
from jellyorganize.scanner.packages import scan


def scan_incoming(config: Config, kind: str, *, include_unassigned: bool = True) -> ScanResult:
    root: Path = config.incoming_root(kind)
    result = scan(root, "auto" if config.incoming.path is not None else kind,
                  minimum_age_seconds=config.incoming.minimum_age_seconds,
                  ignored_extensions=tuple(config.incoming.ignored_extensions),
                  ignored_directories=tuple(config.scanning.ignored_tv_directories))
    if config.incoming.path is not None:
        result.items = [item for item in result.items if item.kind == kind]
        if not include_unassigned:
            result.skipped = []
            result.unassociated = []
    return result
