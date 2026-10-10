"""Explicit movie version labels and conservative sibling collision checks."""

import re

from jellyorganize.scanner.sidecars import MEDIA_EXTENSIONS


def version_of(path):
    prefix = path.parent.name + " - "
    if path.stem.startswith(prefix):
        label = path.stem[len(prefix):]
        if label and label == label.strip() and not re.search(r'[\\/:*?"<>|\x00-\x1f]', label):
            return label
    return None


def label_for(item):
    canonical = version_of(item.path)
    explicit = item.hints.get("version_label")
    edition = item.hints.get("edition")
    resolution = item.hints.get("screen_size")
    if isinstance(edition, list):
        edition = " ".join(str(value) for value in edition)
    label = canonical if canonical is not None else explicit if explicit is not None else (
        " ".join(str(value) for value in (edition, resolution) if value) or "Original")
    if not isinstance(label, str) or len(label) > 80 or re.search(r'[\\/:*?"<>|\x00-\x1f]', label):
        raise ValueError("movie version label must be at most 80 characters without reserved filename characters")
    label = re.sub(r"\s+", " ", label).strip(" .")
    if not label:
        raise ValueError("movie version label is empty")
    return label


def release_key(path):
    label = version_of(path)
    return path.parent, label.casefold() if label else None


def movie_conflict(target, source, ignored=()):
    parent = target.parent
    if not parent.exists():
        return False
    if parent.is_symlink() or not parent.is_dir():
        return True
    label = version_of(target)
    for sibling in parent.iterdir():
        if sibling == source or sibling in ignored or sibling.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if not (sibling.is_file() or sibling.is_symlink()):
            continue
        other = version_of(sibling)
        if sibling.is_symlink() or not label or not other or label.casefold() == other.casefold():
            return True
    return False
