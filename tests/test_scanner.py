from pathlib import Path

from jellyorganize.scanner.incoming import scan_incoming
from jellyorganize.scanner.library import scan_library
from jellyorganize.scanner.sidecars import sidecar_target_name


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")


def test_movie_package_and_sidecars(config):
    package = config.movies.incoming / "Dune.2021.Release"
    touch(package / "Dune.2021.mkv")
    touch(package / "Dune.2021.en.srt")
    touch(package / "poster.jpg")
    result = scan_incoming(config, "movie")
    assert len(result.items) == 1
    assert {path.name for path in result.items[0].sidecars} == {"Dune.2021.en.srt", "poster.jpg"}
    assert result.items[0].hints["year"] == 2021
    assert not result.unassociated


def test_shared_dump_routes_movies_and_episodes_once(config, tmp_path):
    config.incoming.path = tmp_path / "Incoming"
    movie = config.incoming.path / "Dune.2021.Release" / "Dune.2021.mkv"
    episode = config.incoming.path / "The.Bear.S02E03.mkv"
    unknown = config.incoming.path / "Mystery.mkv"
    touch(movie)
    touch(movie.with_suffix(".en.srt"))
    touch(episode)
    touch(episode.with_suffix(".en.srt"))
    touch(unknown)

    movies = scan_incoming(config, "movie")
    tv = scan_incoming(config, "tv", include_unassigned=False)
    assert {item.path for item in movies.items} == {movie, unknown}
    assert {item.path for item in tv.items} == {episode}
    assert next(item for item in movies.items if item.path == movie).sidecars == [movie.with_suffix(".en.srt")]
    assert tv.items[0].sidecars == [episode.with_suffix(".en.srt")]
    assert next(item for item in movies.items if item.path == unknown).reason.startswith("media type is ambiguous")


def test_shared_dump_detects_episode_from_season_folder(config, tmp_path):
    config.incoming.path = tmp_path / "Incoming"
    episode = config.incoming.path / "The Bear (2022)" / "Season 02" / "03 - Sundae.mkv"
    touch(episode)
    assert [item.path for item in scan_incoming(config, "tv").items] == [episode]


def test_shared_dump_does_not_route_unnumbered_episode_as_movie(config, tmp_path):
    config.incoming.path = tmp_path / "Incoming"
    episode = config.incoming.path / "The Bear (2022)" / "Pilot.mkv"
    touch(episode)
    item = scan_incoming(config, "movie").items[0]
    assert item.path == episode
    assert item.reason.startswith("media type is ambiguous")


def test_season_pack_is_multiple_items(config):
    package = config.tv.incoming / "Show.Release"
    touch(package / "Show.S01E01.mkv")
    touch(package / "Show.S01E01.en.srt")
    touch(package / "Show.S01E02.mkv")
    result = scan_incoming(config, "tv")
    assert len(result.items) == 2
    assert [len(item.sidecars) for item in result.items] == [1, 0]


def test_library_path_hints_and_symlinks(config, tmp_path):
    episode = config.tv.library / "The Bear (2022) [tmdbid-136315]" / "Season 02" / "The.Bear.S02E03.mkv"
    touch(episode)
    external = tmp_path / "external.mkv"
    touch(external)
    (episode.parent / "link.mkv").symlink_to(external)
    result = scan_library(config, "tv")
    assert len(result.items) == 1
    assert result.items[0].hints["tmdb_id"] == "136315"
    assert result.items[0].hints["folder_year"] == 2022
    assert result.items[0].hints["folder_season"] == 2
    assert any(reason == "symlink" for _, reason in result.skipped)


def test_recent_and_temporary_files_are_skipped(config):
    config.incoming.minimum_age_seconds = 60
    touch(config.movies.incoming / "Dune.2021.mkv")
    touch(config.movies.incoming / "Dune.2021.part")
    result = scan_incoming(config, "movie")
    assert not result.items
    assert {reason for _, reason in result.skipped} == {"possibly incomplete", "temporary extension"}


def test_unassociated_sidecar_blocks_package(config):
    package = config.tv.incoming / "Show.Release"
    touch(package / "Show.S01E01.mkv")
    touch(package / "Show.S01E02.mkv")
    touch(package / "poster.jpg")
    result = scan_incoming(config, "tv")
    assert len(result.items) == 2
    assert all(item.reason for item in result.items)
    assert result.unassociated == [package / "poster.jpg"]


def test_shortened_subtitle_stem_preserves_language_and_flags():
    assert sidecar_target_name(Path("Show.S01E01.de.forced.srt"), Path("Show.S01E01.1080p.mkv"),
                               "Show - S01E01 - Pilot") == "Show - S01E01 - Pilot.de.forced.srt"


def test_wrong_episode_sidecar_is_not_attached(config):
    package = config.tv.incoming / "Show.Release"
    touch(package / "Show.S01E01.mkv")
    touch(package / "Show.S01E02.en.srt")
    result = scan_incoming(config, "tv")
    assert not result.items[0].sidecars
    assert result.items[0].reason
    assert result.unassociated == [package / "Show.S01E02.en.srt"]


def test_temporary_file_blocks_entire_package(config):
    package = config.movies.incoming / "Dune.Release"
    touch(package / "Dune.2021.mkv")
    touch(package / "download.part")
    result = scan_incoming(config, "movie")
    assert result.items[0].reason


def test_featurettes_directory_is_ignored_but_season_zero_is_scanned(config):
    series = config.tv.library / "Doctor Who (2005)"
    featurette = series / "S01" / "Featurettes" / "Featurette - BBC Breakfast Interview.mkv"
    subtitle = featurette.with_suffix(".srt")
    special = series / "Season 00" / "Doctor.Who.S00E01.mkv"
    touch(featurette)
    touch(subtitle)
    touch(special)

    result = scan_library(config, "tv")
    assert {item.path for item in result.items} == {featurette, special}
    special_item = next(item for item in result.items if item.path == special)
    extra_item = next(item for item in result.items if item.path == featurette)
    assert special_item.hints["season"] == 0
    assert special_item.hints["episode"] == [1]
    assert special_item.reason is None
    assert extra_item.reason == "ignored TV extra"
    assert extra_item.sidecars == [subtitle]
    assert result.unassociated == []
    assert result.skipped == []
    assert featurette.exists() and subtitle.exists()


def test_ignored_tv_directory_is_configurable(config):
    config.scanning.ignored_tv_directories = ["Bonus"]
    featurette = config.tv.library / "Show" / "Featurettes" / "Interview.mkv"
    bonus = config.tv.library / "Show" / "Bonus" / "Interview.mkv"
    touch(featurette)
    touch(bonus)
    result = scan_library(config, "tv")
    assert {item.path for item in result.items} == {featurette, bonus}
    assert next(item for item in result.items if item.path == featurette).reason is None
    assert next(item for item in result.items if item.path == bonus).reason == "ignored TV extra"


def test_numbered_special_inside_featurettes_is_still_eligible(config):
    folder = config.tv.library / "Show (2020)" / "Featurettes"
    special = folder / "Show.S00E02.mkv"
    unrelated = folder / "Featurette - Interview.mkv"
    touch(special)
    touch(unrelated)
    result = scan_library(config, "tv")
    assert next(item for item in result.items if item.path == special).reason is None
    assert next(item for item in result.items if item.path == unrelated).reason == "ignored TV extra"


def test_unassociated_bonus_sidecar_does_not_require_review(config):
    folder = config.tv.library / "Show (2020)" / "Featurettes"
    touch(folder / "Interview.mkv")
    touch(folder / "notes.nfo")
    result = scan_library(config, "tv")
    assert result.items[0].reason == "ignored TV extra"
    assert result.unassociated == []
    assert result.skipped == [(folder / "notes.nfo", "ignored TV extra sidecar")]


def test_unassociated_sidecar_near_numbered_special_blocks_it(config):
    folder = config.tv.library / "Show (2020)" / "Featurettes"
    special = folder / "Show.S00E02.mkv"
    touch(special)
    touch(folder / "mystery.srt")
    result = scan_library(config, "tv")
    assert result.items[0].path == special
    assert result.items[0].reason == "package has temporary, incomplete, or unassociated files"
    assert result.unassociated == [folder / "mystery.srt"]
