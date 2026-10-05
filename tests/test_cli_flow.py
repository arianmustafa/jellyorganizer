from pathlib import Path
from types import SimpleNamespace

from jellyorganize import cli
from jellyorganize.models import Candidate
from jellyorganize.planning.store import PlanStore
from jellyorganize.config import load_config


class FakeTMDb:
    def __init__(self, token, cache, client):
        pass

    async def search_movie(self, title, year=None):
        return [Candidate(provider="tmdb", provider_id="1100099", kind="movie", title="We Live in Time", year=2024)]

    async def get_movie(self, movie_id):
        return Candidate(provider="tmdb", provider_id=movie_id, kind="movie", title="We Live in Time", year=2024)


def test_cli_review_identify_new_plan_and_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "TMDbProvider", FakeTMDb)
    incoming = tmp_path / "Incoming" / "Movies"
    library = tmp_path / "Movies"
    incoming.mkdir(parents=True)
    library.mkdir()
    source = incoming / "we.live.in.time.2024.1080p.web.mkv"
    source.write_bytes(b"fixture")
    config = tmp_path / "config.toml"
    config.write_text(f'[movies]\nincoming = "{incoming}"\nlibrary = "{library}"\n'
                      '[incoming]\nminimum_age_seconds = 0\nstability_seconds = 0\n[providers]\ntvmaze = false\n'
                      '[matching]\nconfirm_exact_movies = false\n')

    assert cli.main(["--config", str(config), "ingest", "movies"]) == 2
    first_output = capsys.readouterr().out
    first_id = next(line.split()[1] for line in first_output.splitlines() if line.startswith("Plan:"))
    assert "[REVIEW]" in first_output
    assert cli.main(["--config", str(config), "review", first_id]) == 2
    assert "identify" in capsys.readouterr().out

    assert cli.main(["--config", str(config), "identify", str(source), "--tmdb", "1100099"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(config), "ingest", "movies"]) == 0
    second_output = capsys.readouterr().out
    second_id = next(line.split()[1] for line in second_output.splitlines() if line.startswith("Plan:"))
    assert second_id != first_id and "[CONFIRMED]" in second_output
    assert cli.main(["--config", str(config), "apply", second_id]) == 0
    assert "APPLIED: 1" in capsys.readouterr().out
    assert not source.exists()
    assert (library / "We Live in Time (2024) [tmdbid-1100099]" / "We Live in Time (2024).mkv").read_bytes() == b"fixture"
    assert cli.main(["--config", str(config), "ingest", "movies"]) == 0
    assert "CONFIRMED: 0" in capsys.readouterr().out


def test_interactive_review_creates_new_plan_without_changing_original(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "TMDbProvider", FakeTMDb)
    incoming = tmp_path / "Incoming" / "Movies"
    library = tmp_path / "Movies"
    incoming.mkdir(parents=True)
    library.mkdir()
    (incoming / "we.live.in.time.2024.mkv").write_bytes(b"fixture")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[movies]\nincoming = "{incoming}"\nlibrary = "{library}"\n'
                           '[incoming]\nminimum_age_seconds = 0\nstability_seconds = 0\n[providers]\ntvmaze = false\n'
                           '[matching]\nconfirm_exact_movies = false\n')
    assert cli.main(["--config", str(config_path), "ingest", "movies"]) == 2
    initial_output = capsys.readouterr().out
    first_id = next(line.split()[1] for line in initial_output.splitlines() if line.startswith("Plan:"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli.main(["--config", str(config_path), "review", first_id]) == 0
    reviewed_output = capsys.readouterr().out
    second_id = next(line.split()[1] for line in reviewed_output.splitlines() if line.startswith("Plan:"))
    store = PlanStore(load_config(config_path).state_dir / "plans")
    assert store.load(first_id).entries[0].status == "REVIEW"
    assert store.load(second_id).entries[0].status == "CONFIRMED"


def test_cli_audit_review_and_apply_existing_movie(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "TMDbProvider", FakeTMDb)
    incoming = tmp_path / "Incoming" / "Movies"
    library = tmp_path / "Movies"
    incoming.mkdir(parents=True)
    source = library / "We Live in Time (2024)" / "we.live.in.time.2024.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[movies]\nincoming = "{incoming}"\nlibrary = "{library}"\n'
                           '[incoming]\nminimum_age_seconds = 0\n[providers]\ntvmaze = false\n')

    assert cli.main(["--config", str(config_path), "audit", "movies"]) == 2
    output = capsys.readouterr().out
    assert "Workflow: AUDIT" in output and "[REVIEW]" in output
    first_id = next(line.split()[1] for line in output.splitlines() if line.startswith("Plan:"))
    assert source.exists()

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli.main(["--config", str(config_path), "review", first_id]) == 0
    reviewed = capsys.readouterr().out
    second_id = next(line.split()[1] for line in reviewed.splitlines() if line.startswith("Plan:"))
    store = PlanStore(load_config(config_path).state_dir / "plans")
    assert store.load(first_id).workflow == "audit"
    assert store.load(first_id).entries[0].status == "REVIEW"
    assert store.load(second_id).entries[0].status == "CONFIRMED"
    assert cli.main(["--config", str(config_path), "apply", second_id]) == 0
    assert "APPLIED: 1" in capsys.readouterr().out
    assert not source.exists()
    target = library / "We Live in Time (2024) [tmdbid-1100099]" / "We Live in Time (2024).mkv"
    assert target.read_bytes() == b"fixture"
    assert cli.main(["--config", str(config_path), "audit", "movies"]) == 0
    assert "[SKIP]" in capsys.readouterr().out


def test_audit_reports_missing_library_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[movies]\nincoming = "{tmp_path / "Incoming" / "Movies"}"\n'
                           f'library = "{tmp_path / "Missing Movies"}"\n')
    assert cli.main(["--config", str(config_path), "audit", "movies"]) == 2
    assert "library root is missing" in capsys.readouterr().out
