import asyncio
from pathlib import Path

import pytest

from jellyorganize.metadata.base import ProviderError
from jellyorganize.metadata.matcher import episode_titles_correspond
from jellyorganize.models import Candidate
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.parsing.guessit_parser import parse_filename
from jellyorganize.parsing.episode_codes import coverage, overlaps
from jellyorganize.planning.audit import plan_audit
from jellyorganize.planning.ingest import plan_ingest
from jellyorganize.planning.store import PlanStore
from jellyorganize.scanner.incoming import scan_incoming
from jellyorganize.scanner.library import scan_library


class FakeTMDb:
    async def search_movie(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="438631", kind="movie", title="Dune", year=2021)]

    async def search_series(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="136315", kind="tv", title="The Bear", year=2022)]

    async def get_series_episode(self, series_id, season, episode):
        return "Sundae" if (season, episode) == (2, 3) else None

    async def get_movie(self, movie_id):
        return Candidate(provider="tmdb", provider_id=movie_id, kind="movie", title="Dune", year=2021)

    async def get_series(self, series_id):
        return Candidate(provider="tmdb", provider_id=series_id, kind="tv", title="The Bear", year=2022)

    async def get_series_external_ids(self, series_id):
        return None, None

    async def get_series_season_episodes(self, series_id, season):
        return {}


class FakeTVmaze:
    def __init__(self, year=2022):
        self.year = year

    async def search_series(self, title, year=None):
        return [Candidate(provider="tvmaze", provider_id="20", kind="tv", title="The Bear", year=self.year)]

    async def get_series_episode(self, series_id, season, episode):
        return "Sundae"


class MrRobotTMDb(FakeTMDb):
    async def search_series(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="62560", kind="tv", title="Mr. Robot", year=2015)]

    async def get_series_episode(self, series_id, season, episode):
        return {1: "eps2.0_unm4sk-pt1.tc", 2: "eps2.0_unm4sk-pt2.tc"}.get(episode) if season == 2 else None

    async def get_series_external_ids(self, series_id):
        return "tt4158110", 289590


class MrRobotTVmaze(FakeTVmaze):
    async def search_series(self, title, year=None):
        return [Candidate(provider="tvmaze", provider_id="1871", kind="tv", title="Mr. Robot", year=2015,
                          imdb_id="tt4158110", tvdb_id=289590)]

    async def get_series_episode(self, series_id, season, episode):
        return {1: "eps2.0_unm4sk-pt1.tc", 2: "eps2.0_unm4sk-pt2.tc"}.get(episode) if season == 2 else None


def mr_robot_source(root):
    return (root / "Mr Robot (2015)" / "Season 2" /
            "Mr.Robot.S02E01-E02.eps2.0.unm4sk-pt1.tc.&.eps2.0.unm4sk-pt2.tc.1080p.10bit.BluRay.AAC5.1.HEVC-Vyndros.mkv")


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unchanged")


def test_movie_proposal_is_read_only(config):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    before = source.stat()
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, FakeTMDb()))
    assert proposals[0].status == "CONFIRMED"
    assert proposals[0].destination.name == "Dune (2021).mkv"
    assert "[tmdbid-438631]" in str(proposals[0].destination)
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert not config.movies.library.exists()


def test_shared_dump_plans_and_applies_movie_and_episode(config, tmp_path):
    config.incoming.path = tmp_path / "Incoming"
    movie = config.incoming.path / "Dune (2021) [tmdbid-438631]" / "Dune.2021.mkv"
    episode = config.incoming.path / "The Bear (2022)" / "The.Bear.S02E03.mkv"
    touch(movie)
    touch(episode)
    config.movies.library.mkdir(parents=True)
    config.tv.library.mkdir(parents=True)

    proposals = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, FakeTMDb()))
    proposals += asyncio.run(plan_ingest(scan_incoming(config, "tv", include_unassigned=False),
                                         config, FakeTMDb(), FakeTVmaze()))
    assert {proposal.item.kind: proposal.status for proposal in proposals} == {
        "movie": "CONFIRMED", "tv": "CONFIRMED"}
    plan = PlanStore(tmp_path / "plans").create(proposals, config, ("movie", "tv"))
    assert plan.source_roots == {"movie": config.incoming.path, "tv": config.incoming.path}
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 2
    assert not movie.exists() and not episode.exists()
    assert all(entry.destination.exists() for entry in plan.entries)


def test_tv_agreement_and_disagreement(config):
    source = config.tv.incoming / "The Bear (2022)" / "The.Bear.S02E03.mkv"
    touch(source)
    scan = scan_incoming(config, "tv")
    agreed = asyncio.run(plan_ingest(scan, config, FakeTMDb(), FakeTVmaze()))[0]
    assert agreed.status == "CONFIRMED"
    assert agreed.candidate.tvmaze_id == "20"
    assert agreed.destination.name == "The Bear - S02E03 - Sundae.mkv"
    disagreed = asyncio.run(plan_ingest(scan, config, FakeTMDb(), FakeTVmaze(2001)))[0]
    assert disagreed.status == "REVIEW"


def test_tv_without_local_year_stays_review_despite_provider_agreement(config):
    source = config.tv.incoming / "The.Bear.S02E03.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, FakeTMDb(), FakeTVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.confidence == 0.95


def test_tv_other_version_does_not_disagree_with_explicit_local_year(config):
    class MultipleVersions(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="19", kind="tv", title="The Bear", year=2001),
                    Candidate(provider="tvmaze", provider_id="20", kind="tv", title="The Bear", year=2022)]

    source = config.tv.incoming / "The Bear (2022)" / "The.Bear.S02E03.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, FakeTMDb(), MultipleVersions()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.candidate.tvmaze_id == "20"


def test_tv_yearless_episode_title_agreement_is_strong_evidence(config):
    source = config.tv.incoming / "The Bear" / "The Bear - S02E03 - Sundae.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, FakeTMDb(), FakeTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 0.99
    assert proposal.reason == "local episode title agrees with TMDb and TVmaze"


def test_tv_yearless_episode_title_disagreement_stays_review(config):
    source = config.tv.incoming / "The Bear" / "The Bear - S02E03 - Sandwich.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, FakeTMDb(), FakeTVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "local episode title disagrees with TMDb"
    assert proposal.confidence == 0.75


def test_tv_alt_provider_numbering_uses_unique_episode_title_and_series_ids(config):
    class FuturamaTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="615", kind="tv", title="Futurama", year=1999)]

        async def get_series_episode(self, series_id, season, episode):
            return "The Impossible Stream" if (season, episode) == (8, 1) else None

        async def get_series_external_ids(self, series_id):
            return "tt0149460", 73871

    class FuturamaTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="538", kind="tv", title="Futurama", year=1999,
                              imdb_id="tt0149460", tvdb_id=73871)]

        async def get_series_episode(self, series_id, season, episode):
            return None  # No same-number episode in this fixture.

        async def get_series_episodes(self, series_id):
            return [(8, 1, "Rebirth"), (11, 1, "The Impossible Stream")]

    source = (config.tv.incoming / "Futurama" / "S08" /
              "Futurama.S08E01.The.Impossible.Stream.2160p.HULU.WEB-DL.DDP5.1.H.265-FLUX.mkv")
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, FuturamaTMDb(), FuturamaTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 0.99
    assert proposal.destination.name == "Futurama - S08E01 - The Impossible Stream.mkv"
    assert "TVmaze S11E01" in proposal.reason

    class NoExternalIds(FuturamaTMDb):
        async def get_series_external_ids(self, series_id):
            return None, None

    unresolved = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                         NoExternalIds(), FuturamaTVmaze()))[0]
    assert unresolved.status == "REVIEW"
    assert unresolved.reason == "TVmaze alternate numbering; external IDs unavailable"


def test_tv_alt_provider_numbering_with_ambiguous_title_stays_review(config):
    class TMDb(FakeTMDb):
        async def get_series_external_ids(self, series_id):
            return "tt14452776", None

    class TVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="20", kind="tv", title="The Bear", year=2022,
                              imdb_id="tt14452776")]

        async def get_series_episode(self, series_id, season, episode):
            return None

        async def get_series_episodes(self, series_id):
            return [(9, 1, "Sundae"), (10, 1, "Sundae")]

    source = config.tv.incoming / "The Bear" / "The Bear - S02E03 - Sundae.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, TMDb(), TVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "TVmaze alternate episode title missing or ambiguous"


def test_tv_same_number_different_title_without_local_title_stays_review(config):
    class TMDb(FakeTMDb):
        async def get_series_external_ids(self, series_id):
            return "tt14452776", None

    class TVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="20", kind="tv", title="The Bear", year=2022,
                              imdb_id="tt14452776")]

        async def get_series_episode(self, series_id, season, episode):
            return "A Different Episode"

    source = config.tv.incoming / "The.Bear.S02E03.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, TMDb(), TVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "TVmaze same-number episode title differs"


def test_tv_yearless_other_version_stays_review_even_with_episode_title(config):
    class MultipleVersions(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="19", kind="tv", title="The Bear", year=2001),
                    Candidate(provider="tvmaze", provider_id="20", kind="tv", title="The Bear", year=2022)]

    source = config.tv.incoming / "The Bear" / "The Bear - S02E03 - Sundae.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, FakeTMDb(), MultipleVersions()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "TVmaze year disagrees with TMDb"


def test_tv_yearless_series_prefix_in_episode_title(config):
    class BoJackTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="61222", kind="tv", title="BoJack Horseman", year=2014)]

        async def get_series_episode(self, series_id, season, episode):
            return "BoJack Horseman: The BoJack Horseman Story, Chapter One"

    class BoJackTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="184", kind="tv", title="BoJack Horseman", year=2014)]

        async def get_series_episode(self, series_id, season, episode):
            return "The BoJack Horseman Story, Chapter One"

    source = (config.tv.incoming / "BoJack Horseman" / "Season 1" /
              "BoJack Horseman - S01E01 - BoJack Horseman - The BoJack Horseman Story, Chapter One - "
              "[WEBRip-1080p x265] [EAC3 5.1].mkv")
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, BoJackTMDb(), BoJackTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 0.99


def test_tv_yearless_prefix_cannot_mask_different_episode_title():
    assert not episode_titles_correspond("BoJack Horseman - A Different Story",
                                         "BoJack Horseman: The BoJack Horseman Story, Chapter One",
                                         "The BoJack Horseman Story, Chapter One", "BoJack Horseman")
    assert not episode_titles_correspond("BoJack Horseman - The BoJack Horseman Story, Chapter One",
                                         "BoJack Horseman: The BoJack Horseman Story, Chapter One",
                                         "Another Story", "BoJack Horseman")


def test_tv_release_tag_does_not_truncate_episode_title(config):
    class BoJackTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="61222", kind="tv", title="BoJack Horseman", year=2014)]

        async def get_series_episode(self, series_id, season, episode):
            return "Ancient History"

    class BoJackTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="184", kind="tv", title="BoJack Horseman", year=2014)]

        async def get_series_episode(self, series_id, season, episode):
            return "Ancient History"

    source = (config.tv.incoming / "BoJack Horseman" / "Season 5" /
              "BoJack Horseman - S05E09 - Ancient History - [WEBRip-1080p x265] [EAC3 5.1].mkv")
    touch(source)
    scan = scan_incoming(config, "tv")
    assert scan.items[0].hints["episode_title"] == "Ancient History"
    proposal = asyncio.run(plan_ingest(scan, config, BoJackTMDb(), BoJackTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 0.99


def test_numbered_episode_title_before_parenthesized_release_is_preserved(config):
    class DoctorWhoTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="57243", kind="tv", title="Doctor Who", year=2005)]

        async def get_series_episode(self, series_id, season, episode):
            return "Aliens of London (1)"

    class DoctorWhoTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="210", kind="tv", title="Doctor Who", year=2005)]

        async def get_series_episode(self, series_id, season, episode):
            return "Aliens of London"

    source = (config.tv.incoming / "Doctor Who (2005)" / "S01" /
              "Doctor Who (2005) - S01E04 - Aliens of London (1) (1080p BluRay x265 Panda).mkv")
    touch(source)
    assert parse_filename(source, "tv")["episode_title"] == "Aliens of London (1)"
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                       DoctorWhoTMDb(), DoctorWhoTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.destination.name == "Doctor Who - S01E04 - Aliens of London (1).mkv"


def test_numbered_episode_suffix_cannot_mask_different_title():
    assert not episode_titles_correspond("World War Three (1)", "Aliens of London (1)",
                                         "Aliens of London", "Doctor Who")
    assert not episode_titles_correspond("Aliens of London (2)", "Aliens of London (1)",
                                         "Aliens of London", "Doctor Who")


def test_combined_mr_robot_episodes_are_confirmed_and_named_for_jellyfin(config):
    source = mr_robot_source(config.tv.incoming)
    touch(source)
    subtitle = source.with_name(source.stem + ".en.srt")
    touch(subtitle)
    scan = scan_incoming(config, "tv")
    assert scan.items[0].hints["episode"] == [1, 2]
    assert scan.items[0].sidecars == [subtitle]
    proposal = asyncio.run(plan_ingest(scan, config, MrRobotTMDb(), MrRobotTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 0.99
    assert proposal.destination.name == "Mr. Robot - S02E01-E02.mkv"
    assert proposal.destination.parent.name == "Season 02"
    assert proposal.candidate.tvmaze_id == "1871"
    assert "S02E01-E02" in proposal.reason
    assert proposal.sidecar_destinations[subtitle].name == "Mr. Robot - S02E01-E02.en.srt"


def test_combined_episodes_stay_review_without_full_provider_agreement(config):
    source = mr_robot_source(config.tv.incoming)
    touch(source)

    class MissingEpisode(MrRobotTVmaze):
        async def get_series_episode(self, series_id, season, episode):
            return None if episode == 2 else await super().get_series_episode(series_id, season, episode)

    missing = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                     MrRobotTMDb(), MissingEpisode()))[0]
    assert missing.status == "REVIEW"
    assert missing.reason == "TVmaze multi-episode number missing"

    class WrongTitle(MrRobotTVmaze):
        async def get_series_episode(self, series_id, season, episode):
            return "unrelated" if episode == 2 else await super().get_series_episode(series_id, season, episode)

    wrong = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                   MrRobotTMDb(), WrongTitle()))[0]
    assert wrong.status == "REVIEW"
    assert wrong.reason == "TMDb and TVmaze multi-episode titles disagree"

    class WrongId(MrRobotTVmaze):
        async def search_series(self, title, year=None):
            rows = await super().search_series(title, year)
            return [rows[0].model_copy(update={"imdb_id": "tt0000000"})]

    wrong_id = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                      MrRobotTMDb(), WrongId()))[0]
    assert wrong_id.status == "REVIEW"
    assert wrong_id.reason == "TMDb and TVmaze external IDs disagree"

    class NoSharedId(MrRobotTVmaze):
        async def search_series(self, title, year=None):
            rows = await super().search_series(title, year)
            return [rows[0].model_copy(update={"imdb_id": None, "tvdb_id": None})]

    no_id = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                   MrRobotTMDb(), NoSharedId()))[0]
    assert no_id.status == "REVIEW"
    assert no_id.reason == "multi-episode series external IDs unavailable"


def test_combined_episode_compact_code_is_supported(config):
    source = config.tv.incoming / "Mr Robot (2015)" / "Mr.Robot.S02E01E02.mkv"
    touch(source)
    assert parse_filename(source, "tv")["episode"] == [1, 2]
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                       MrRobotTMDb(), MrRobotTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.destination.name == "Mr. Robot - S02E01-E02.mkv"


def test_combined_episode_shared_part_title_matches_parks_and_recreation(config, tmp_path):
    class ParksTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="8592", kind="tv",
                              title="Parks and Recreation", year=2009)]

        async def get_series_episode(self, series_id, season, episode):
            return {1: "London", 2: "The Pawnee-Eagleton Tip-Off Classic"}.get(episode) if season == 6 else None

        async def get_series_external_ids(self, series_id):
            return "tt1266020", 84912

    class ParksTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="174", kind="tv",
                              title="Parks and Recreation", year=2009,
                              imdb_id="tt1266020", tvdb_id=84912)]

        async def get_series_episode(self, series_id, season, episode):
            return {1: "London (1)", 2: "London (2)",
                    3: "The Pawnee-Eagleton Tip Off Classic"}.get(episode) if season == 6 else None

    source = (config.tv.incoming / "Parks and Recreation (2009) Season 1-7 S01-S07 (1080p AMZN WEBRip x265 HEVC 10bit AAC 5.1 Silence)" /
              "Season 6" / "Parks and Recreation (2009) - S06E01-E02 - London (1080p AMZN WEBRip x265 Silence).mkv")
    touch(source)
    config.tv.library.mkdir(parents=True)
    scan = scan_incoming(config, "tv")
    assert scan.items[0].hints["episode_title"] == "London"
    proposal = asyncio.run(plan_ingest(scan, config, ParksTMDb(), ParksTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.destination.name == "Parks and Recreation - S06E01 - London.mkv"
    assert "TVmaze S06E01-E02 maps to TMDb S06E01" in proposal.reason
    saved = PlanStore(tmp_path / "plans").create([proposal], config, ("tv",))

    wrong_source = source.with_name(source.name.replace("London", "Paris"))
    source.rename(wrong_source)
    wrong = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                   ParksTMDb(), ParksTVmaze()))[0]
    assert wrong.status == "REVIEW"
    assert wrong.reason == "multi-episode filename titles do not match TMDb"

    wrong_source.rename(source)
    class DifferentSecondPart(ParksTMDb):
        async def get_series_episode(self, series_id, season, episode):
            return "Paris (2)" if episode == 2 else await super().get_series_episode(series_id, season, episode)

    wrong_provider = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                            DifferentSecondPart(), ParksTVmaze()))[0]
    assert wrong_provider.status == "REVIEW"

    class MissingAnchor(ParksTVmaze):
        async def get_series_episode(self, series_id, season, episode):
            return None if episode == 3 else await super().get_series_episode(series_id, season, episode)

    unanchored = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                        ParksTMDb(), MissingAnchor()))[0]
    assert unanchored.status == "REVIEW"

    separate_part = source.with_name("Parks and Recreation (2009) - S06E02 - London (2).mkv")
    touch(separate_part)
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                        ParksTMDb(), ParksTVmaze()))
    combined = next(row for row in proposals if row.item.path == source)
    assert combined.status == "CONFLICT"
    assert combined.reason == "another source file covers the same TV episode"
    _, counts = apply_plan(saved, config, tmp_path / "transactions")
    assert counts["CONFLICT"] == 1
    assert source.exists() and separate_part.exists()

    separate_part.unlink()
    audit_source = config.tv.library / source.relative_to(config.tv.incoming)
    audit_source.parent.mkdir(parents=True)
    source.rename(audit_source)
    audit_proposals = asyncio.run(plan_audit(scan_library(config, "tv"), config,
                                             ParksTMDb(), ParksTVmaze()))
    assert len(audit_proposals) == 1
    assert audit_proposals[0].status == "CONFIRMED"
    assert audit_proposals[0].destination.name == "Parks and Recreation - S06E01 - London.mkv"
    audit_plan = PlanStore(tmp_path / "plans").create(audit_proposals, config, ("tv",), workflow="audit")
    _, audit_counts = apply_plan(audit_plan, config, tmp_path / "transactions")
    assert audit_counts["APPLIED"] == 1
    assert not audit_source.exists()
    repeated = asyncio.run(plan_audit(scan_library(config, "tv"), config, None))
    assert len(repeated) == 1
    assert repeated[0].status == "SKIP"
    assert repeated[0].reason == "already canonical"


@pytest.mark.parametrize("season,source_start,target_number,finale,previous", [
    (6, 21, 20, "Moving Up", "One in 8,000"),
    (7, 12, 12, "One Last Ride", "Two Funerals"),
])
def test_combined_season_finale_maps_to_single_tmdb_episode(
        config, tmp_path, season, source_start, target_number, finale, previous):
    class ParksTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="8592", kind="tv",
                              title="Parks and Recreation", year=2009)]

        async def get_series_episode(self, series_id, requested_season, episode):
            return ({target_number - 1: previous, target_number: finale}.get(episode)
                    if requested_season == season else None)

        async def get_series_season_episodes(self, series_id, requested_season):
            return ({target_number - 1: previous, target_number: finale}
                    if requested_season == season else {})

        async def get_series_external_ids(self, series_id):
            return "tt1266020", 84912

    class ParksTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="174", kind="tv",
                              title="Parks and Recreation", year=2009,
                              imdb_id="tt1266020", tvdb_id=84912)]

        async def get_series_episode(self, series_id, requested_season, episode):
            return ({source_start - 1: previous, source_start: f"{finale} (1)",
                     source_start + 1: f"{finale} (2)"}.get(episode)
                    if requested_season == season else None)

    source = (config.tv.library / "Parks and Recreation (2009) Season 1-7" /
              f"Season {season}" /
              f"Parks and Recreation (2009) - S{season:02d}E{source_start:02d}-E{source_start + 1:02d} - {finale} (1080p AMZN WEBRip x265 Silence).mkv")
    touch(source)
    scan = scan_library(config, "tv")
    assert scan.items[0].hints["episode_title"] == finale
    proposal = asyncio.run(plan_audit(scan, config, ParksTMDb(), ParksTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.destination.name == f"Parks and Recreation - S{season:02d}E{target_number:02d} - {finale}.mkv"
    assert "finale, previous episode, and series IDs agree" in proposal.reason

    class WrongPrevious(ParksTVmaze):
        async def get_series_episode(self, series_id, requested_season, episode):
            if episode == source_start - 1:
                return "Unrelated"
            return await super().get_series_episode(series_id, requested_season, episode)

    assert asyncio.run(plan_audit(scan, config, ParksTMDb(), WrongPrevious()))[0].status == "REVIEW"

    class ExtraMazeEpisode(ParksTVmaze):
        async def get_series_episode(self, series_id, requested_season, episode):
            if episode == source_start + 2:
                return "Extra"
            return await super().get_series_episode(series_id, requested_season, episode)

    assert asyncio.run(plan_audit(scan, config, ParksTMDb(), ExtraMazeEpisode()))[0].status == "REVIEW"

    class ConflictingIDs(ParksTVmaze):
        async def search_series(self, title, year=None):
            rows = await super().search_series(title, year)
            return [rows[0].model_copy(update={"tvdb_id": 1})]

    assert asyncio.run(plan_audit(scan, config, ParksTMDb(), ConflictingIDs()))[0].status == "REVIEW"

    class AmbiguousTMDbTitle(ParksTMDb):
        async def get_series_season_episodes(self, series_id, requested_season):
            episodes = await super().get_series_season_episodes(series_id, requested_season)
            episodes[target_number - 1] = finale
            return episodes

    assert asyncio.run(plan_audit(scan, config, AmbiguousTMDbTitle(), ParksTVmaze()))[0].status == "REVIEW"

    plan = PlanStore(tmp_path / "plans").create([proposal], config, ("tv",), workflow="audit")
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    assert not source.exists()
    assert proposal.destination.exists()
    repeated = asyncio.run(plan_audit(scan_library(config, "tv"), config, None))
    assert len(repeated) == 1 and repeated[0].status == "SKIP"


def test_noncontiguous_multi_episode_code_stays_review(config):
    source = config.tv.incoming / "Mr Robot (2015)" / "Mr.Robot.S02E01E03.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                       MrRobotTMDb(), MrRobotTVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "multi-episode numbers are not a short contiguous range"


def test_combined_episodes_with_conflicting_filename_titles_stay_review(config):
    source = config.tv.incoming / "Mr Robot (2015)" / "Mr.Robot.S02E01-E02.Wrong.Titles.1080p.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                       MrRobotTMDb(), MrRobotTVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "multi-episode filename titles do not match TMDb"


def test_episode_code_coverage_expands_ranges_and_combined_codes():
    assert coverage(Path("Show.S01E01-E03.mkv")) == (1, frozenset({1, 2, 3}))
    assert coverage(Path("Show.S01E01E02.mkv")) == (1, frozenset({1, 2}))
    assert coverage(Path("Show.S01E02.mkv")) == (1, frozenset({2}))
    assert overlaps(Path("Show.S01E02.mkv"), Path("Show.S01E01-E03.mkv"))
    assert not overlaps(Path("Show.S01E04.mkv"), Path("Show.S01E01-E03.mkv"))


def test_combined_episode_conflicts_with_existing_single_and_other_proposal(config):
    source = mr_robot_source(config.tv.incoming)
    touch(source)
    destination_parent = config.tv.library / "Mr. Robot (2015) [tmdbid-62560]" / "Season 02"
    single = destination_parent / "Mr. Robot - S02E02 - eps2.0_unm4sk-pt2.tc.mkv"
    touch(single)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                       MrRobotTMDb(), MrRobotTVmaze()))[0]
    assert proposal.status == "CONFLICT"
    assert source.exists() and single.exists()

    single.unlink()
    second_source = config.tv.incoming / "Mr Robot (2015)" / "Mr.Robot.S02E02.mkv"
    touch(second_source)
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                        MrRobotTMDb(), MrRobotTVmaze()))
    assert {proposal.status for proposal in proposals} == {"CONFLICT"}
    assert all(proposal.reason == "multiple items cover the same TV episode" for proposal in proposals)


def test_combined_episode_apply_rechecks_overlap_created_after_plan(config, tmp_path):
    source = mr_robot_source(config.tv.incoming)
    touch(source)
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config,
                                        MrRobotTMDb(), MrRobotTVmaze()))
    plan = PlanStore(tmp_path / "plans").create(proposals, config, ("tv",))
    existing = plan.entries[0].destination.parent / "Mr. Robot - S02E01 - First.mkv"
    touch(existing)
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["CONFLICT"] == 1
    assert source.exists() and existing.exists()
    assert not plan.entries[0].destination.exists()


def test_combined_episode_audit_moves_and_then_skips_canonical(config, tmp_path):
    source = mr_robot_source(config.tv.library)
    touch(source)
    proposals = asyncio.run(plan_audit(scan_library(config, "tv"), config,
                                       MrRobotTMDb(), MrRobotTVmaze()))
    assert proposals[0].status == "CONFIRMED"
    plan = PlanStore(tmp_path / "plans").create(proposals, config, ("tv",), workflow="audit")
    _, counts = apply_plan(plan, config, tmp_path / "transactions")
    assert counts["APPLIED"] == 1
    assert plan.entries[0].destination.exists()
    assert not source.exists()
    repeated = asyncio.run(plan_audit(scan_library(config, "tv"), config, None))
    assert repeated[0].status == "SKIP"
    assert repeated[0].reason == "already canonical"


def test_tv_yearless_external_ids_confirm_episode_without_local_title(config):
    class CommunityTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="18347", kind="tv", title="Community", year=2009)]

        async def get_series_episode(self, series_id, season, episode):
            return "Pilot"

        async def get_series_external_ids(self, series_id):
            return "tt1439629", 94571

    class CommunityTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="318", kind="tv", title="Community", year=2009,
                              imdb_id="tt1439629", tvdb_id=94571)]

        async def get_series_episode(self, series_id, season, episode):
            return "Pilot"

    source = config.tv.incoming / "Community" / "S01" / "Community.S01E01.REPACK.1080p.Bluray.x265-HiQVE.mkv"
    touch(source)
    scan = scan_incoming(config, "tv")
    assert "episode_title" not in scan.items[0].hints
    proposal = asyncio.run(plan_ingest(scan, config, CommunityTMDb(), CommunityTVmaze()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 0.99
    assert proposal.reason == "TMDb and TVmaze external IDs agree"


def test_tv_yearless_conflicting_external_ids_stay_review(config):
    class CommunityTMDb(FakeTMDb):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tmdb", provider_id="18347", kind="tv", title="Community", year=2009)]

        async def get_series_episode(self, series_id, season, episode):
            return "Pilot"

        async def get_series_external_ids(self, series_id):
            return "tt1439629", 94571

    class WrongTVmaze(FakeTVmaze):
        async def search_series(self, title, year=None):
            return [Candidate(provider="tvmaze", provider_id="318", kind="tv", title="Community", year=2009,
                              imdb_id="tt1439629", tvdb_id=99999)]

        async def get_series_episode(self, series_id, season, episode):
            return "Pilot"

    source = config.tv.incoming / "Community.S01E01.REPACK.1080p.Bluray.x265-HiQVE.mkv"
    touch(source)
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "tv"), config, CommunityTMDb(), WrongTVmaze()))[0]
    assert proposal.status == "REVIEW"
    assert proposal.reason == "TMDb and TVmaze external IDs disagree"


def test_existing_destination_and_two_sources_conflict(config):
    touch(config.movies.incoming / "Dune.2021.mkv")
    touch(config.movies.incoming / "Dune (2021).mp4")
    scan = scan_incoming(config, "movie")
    proposals = asyncio.run(plan_ingest(scan, config, FakeTMDb()))
    assert {proposal.status for proposal in proposals} == {"CONFLICT"}  # extensions differ, identity is the same
    touch(proposals[0].destination)
    proposals = asyncio.run(plan_ingest(scan, config, FakeTMDb()))
    assert proposals[0].status == "CONFLICT"


def test_explicit_id_is_verified_without_search(config):
    touch(config.movies.incoming / "Dune (2021) [tmdbid-438631]" / "Dune.mkv")
    proposal = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, FakeTMDb()))[0]
    assert proposal.status == "CONFIRMED"
    assert proposal.confidence == 1.0


def test_provider_failure_does_not_stop_other_items(config):
    class Intermittent(FakeTMDb):
        async def search_movie(self, title, year=None):
            if title == "Crash":
                raise ProviderError("provider timeout")
            return await super().search_movie(title, year)

    touch(config.movies.incoming / "Crash.1996.mkv")
    touch(config.movies.incoming / "Dune.2021.mkv")
    proposals = asyncio.run(plan_ingest(scan_incoming(config, "movie"), config, Intermittent()))
    assert [proposal.status for proposal in proposals] == ["ERROR", "CONFIRMED"]
