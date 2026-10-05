from jellyorganize.config import Config
from jellyorganize.models import ScanResult
from jellyorganize.scanner.packages import scan


def scan_library(config: Config, kind: str) -> ScanResult:
    root = config.movies.library if kind == "movie" else config.tv.library
    return scan(root, kind, minimum_age_seconds=config.incoming.minimum_age_seconds,
                ignored_extensions=tuple(config.incoming.ignored_extensions),
                ignored_directories=tuple(config.scanning.ignored_tv_directories))
