import time
from pathlib import Path

from jellyorganize.identity.store import IdentityStore
from jellyorganize.models import Candidate, MediaItem


def episode(path: Path) -> MediaItem:
    return MediaItem(path=path, kind="tv", root=path.parents[3],
                     hints={"folder_title": "Doctor Who", "folder_year": 2005,
                            "season": 1, "episode": [1]})


def test_permanent_skip_is_one_path_not_an_entire_series(tmp_path):
    store = IdentityStore(tmp_path / "identities.sqlite3")
    extra = episode(tmp_path / "TV Shows" / "Doctor Who (2005)" / "Featurettes" / "Interview.mkv")
    regular = episode(tmp_path / "TV Shows" / "Doctor Who (2005)" / "Season 01" / "Doctor.Who.S01E01.mkv")
    candidate = Candidate(provider="tmdb", provider_id="57243", kind="tv", title="Doctor Who", year=2005)

    store.confirm(regular, candidate, "test")
    store.skip_permanently(extra)
    assert store.lookup(extra) == (candidate, True)
    assert store.lookup(regular) == (candidate, False)
    store.confirm(extra, candidate, "test")
    assert store.lookup(extra) == (candidate, False)


def test_legacy_title_wide_skip_cannot_hide_other_episodes(tmp_path):
    store = IdentityStore(tmp_path / "identities.sqlite3")
    extra = episode(tmp_path / "TV Shows" / "Doctor Who (2005)" / "Featurettes" / "Interview.mkv")
    regular = episode(tmp_path / "TV Shows" / "Doctor Who (2005)" / "Season 01" / "Doctor.Who.S01E01.mkv")
    with store._connect() as connection:
        connection.execute("INSERT INTO identities VALUES (?, ?, ?, NULL, 1, ?, ?)",
                           (*store.key(extra), time.time(), "old permanent skip"))
    assert store.lookup(extra) == (None, False)
    assert store.lookup(regular) == (None, False)
