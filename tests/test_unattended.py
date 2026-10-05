import asyncio
import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from jellyorganize import cli
from jellyorganize.benchmark import evaluate, load_corpus
from jellyorganize.completion import CompletionTracker
from jellyorganize.config import Config, init_config, load_config
from jellyorganize.models import MediaItem, Proposal
from jellyorganize.operations import Operations
from jellyorganize.planning.store import PlanStore
from jellyorganize.service import install, save_credential, token, units
from test_auto import setup


def test_config_creation_is_private_packaged_and_never_overwrites(tmp_path):
    path = init_config(tmp_path / 'settings.toml')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path).schema_version == 1
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        init_config(path)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match='does not exist'):
        load_config(tmp_path / 'absent.toml')


@pytest.mark.parametrize('data', [
    {'incoming': {'unknown': True}}, {'providers': {'omdb': True}},
    {'providers': {'tmdb': False}}, {'filesystem': {'verify_cross_filesystem_copy': False}},
    {'filesystem': {'hash_algorithm': 'md5'}}, {'incoming': {'path': 'relative'}},
    {'schema_version': 2}, {'service': {'interval_seconds': 1}},
])
def test_config_rejects_unsupported_and_misspelled_settings(data):
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_legacy_configuration_is_explicitly_migrated():
    with pytest.warns(UserWarning, match='retired'):
        config = Config.model_validate({'matching': {'review_threshold': 0.8},
                                       'movies': {'incoming': '/old/incoming', 'library': '/old/movies'}})
    assert config.incoming.path is None
    assert config.incoming_root('movie') == Path('/old/incoming')


@pytest.mark.parametrize('mode', ['stable', 'marker'])
def test_completion_tracks_media_and_sidecar_changes(tmp_path, mode):
    video = tmp_path / 'Dune.2021.mkv'
    sidecar = video.with_suffix('.en.srt')
    video.write_bytes(b'video')
    sidecar.write_bytes(b'subtitle')
    item = MediaItem(path=video, root=tmp_path, kind='movie', sidecars=[sidecar])
    config = Config()
    config.incoming.completion = mode
    tracker = CompletionTracker(tmp_path / 'completion.sqlite3')
    assert not tracker.eligible(item, config.incoming, now=100)
    assert not tracker.eligible(item, config.incoming, now=159)
    assert tracker.eligible(item, config.incoming, now=160) == (mode == 'stable')
    tracker.acknowledge(item)
    assert tracker.eligible(item, config.incoming)
    sidecar.write_bytes(b'changed subtitle')
    assert not tracker.eligible(item, config.incoming)
    tracker.acknowledge(item)
    video.write_bytes(b'changed video')
    assert not tracker.eligible(item, config.incoming)


def test_run_retries_deduplicates_exceptions_and_keeps_processing(tmp_path, monkeypatch, capsys):
    path, roots = setup(tmp_path, monkeypatch)
    with path.open('a') as stream:
        stream.write('[matching]\nconfirm_exact_movies = false\n')
    movie = roots['movie_in'] / 'We.Live.in.Time.2024.mkv'
    movie.write_bytes(b'movie')
    assert cli.main(['--config', str(path), 'run', 'movies']) == 2
    first = json.loads(capsys.readouterr().out)
    assert first['changed_exceptions'] == 1
    assert cli.main(['--config', str(path), 'run', 'movies']) == 2
    assert json.loads(capsys.readouterr().out)['changed_exceptions'] == 0
    operations = Operations(load_config(path).state_dir / 'operations.sqlite3')
    assert operations.exceptions()[0]['occurrences'] == 2
    path.write_text(path.read_text().replace('confirm_exact_movies = false', 'confirm_exact_movies = true'))
    assert cli.main(['--config', str(path), 'run', 'movies']) == 0
    assert json.loads(capsys.readouterr().out)['apply']['APPLIED'] == 1
    assert operations.exceptions() == []
    assert not movie.exists()
    assert operations.status()['last_run']['exit_code'] == 0
    assert cli.main(['--config', str(path), 'run', 'movies']) == 0
    assert not json.loads(capsys.readouterr().out)['apply']


def test_marker_ready_cli_moves_only_acknowledged_unchanged_file(tmp_path, monkeypatch, capsys):
    path, roots = setup(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace('stability_seconds = 0', 'completion = "marker"'))
    movie = roots['movie_in'] / 'We.Live.in.Time.2024.mkv'
    movie.write_bytes(b'movie')
    assert cli.main(['--config', str(path), 'run', 'movies']) == 0
    assert movie.exists()
    assert cli.main(['--config', str(path), 'ready', str(movie)]) == 0
    assert cli.main(['--config', str(path), 'run', 'movies']) == 0
    assert not movie.exists()
    capsys.readouterr()


def test_service_saves_environment_credential_privately_and_never_overwrites(config, tmp_path, monkeypatch):
    config.service.credential_file = tmp_path / 'secrets' / 'token'
    monkeypatch.setenv('TMDB_API_TOKEN', 'test-token-not-real')
    path = init_config(tmp_path / 'config.toml')
    directory = tmp_path / 'units'
    install(config, path, directory, save_token=True)
    assert stat.S_IMODE(config.service.credential_file.stat().st_mode) == 0o600
    monkeypatch.delenv('TMDB_API_TOKEN')
    assert token(config) == 'test-token-not-real'
    assert install(config, path, directory) == directory
    monkeypatch.setenv('TMDB_API_TOKEN', 'different-token')
    with pytest.raises(FileExistsError, match='differs'):
        save_credential(config)
    monkeypatch.delenv('TMDB_API_TOKEN')
    config.service.credential_file.chmod(0o644)
    with pytest.raises(ValueError, match='chmod 600'):
        token(config)
    config.service.credential_file.unlink()
    config.service.credential_file.symlink_to(path)
    with pytest.raises(OSError):
        token(config)
    (directory / 'jellyorganize.service').write_text('unrelated unit')
    with pytest.raises(FileExistsError):
        install(config, path, directory)
    assert (directory / 'jellyorganize.service').read_text() == 'unrelated unit'


def test_service_escapes_systemd_expansion(config):
    service = units(config, Path('/tmp/a %n $HOME "file".toml'))['jellyorganize.service']
    assert '%%n' in service and '$$HOME' in service and '\\"file\\"' in service
    with pytest.raises(ValueError):
        units(config, Path('/tmp/new\nline'))


def test_matching_benchmark_gate_and_wrong_ground_truth(tmp_path):
    result = asyncio.run(evaluate())
    assert result['cases'] >= 40 and result['automatic_matches'] >= 15
    assert result['passed'] and result['wrong_automatic_matches'] == 0
    corpus = load_corpus()
    case = next(case for case in corpus['cases'] if case['should_auto'])
    case['expected']['tmdb_id'] = '1'
    corpus['cases'] = [case]
    path = tmp_path / 'bad-label.json'
    path.write_text(json.dumps(corpus))
    result = asyncio.run(evaluate(corpus_path=path))
    assert not result['passed'] and result['wrong_automatic_matches'] == 1


def test_run_reports_provider_failure_even_alongside_conflict(tmp_path, monkeypatch, capsys):
    from jellyorganize.metadata.base import ProviderError
    from test_auto import FakeTMDb
    path, roots = setup(tmp_path, monkeypatch)
    class IntermittentTMDb(FakeTMDb):
        async def get_series_episode(self, identity, season, episode):
            if episode == 3:
                raise ProviderError('provider temporarily unavailable')
            return await super().get_series_episode(identity, season, episode)
    monkeypatch.setattr(cli, 'TMDbProvider', IntermittentTMDb)
    (roots['movie_in'] / 'We.Live.in.Time.2024.mkv').write_bytes(b'movie')
    package = roots['tv_in'] / 'The Bear (2022)'
    package.mkdir()
    (package / 'The.Bear.S01E01.mkv').write_bytes(b'duplicate')
    (package / 'The.Bear.S02E03.mkv').write_bytes(b'retry')
    occupied = roots['tv_lib'] / 'The Bear (2022) [tmdbid-136315]' / 'Season 01' / 'The Bear - S01E01 - Pilot.mkv'
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b'existing')
    assert cli.main(['--config', str(path), 'run']) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary['apply']['APPLIED'] == 1
    assert summary['counts']['CONFLICT'] == 1 and summary['counts']['ERROR'] == 1
    operations = Operations(load_config(path).state_dir / 'operations.sqlite3')
    assert operations.status()['last_healthy_run'] is None


def test_future_plan_and_journal_versions_fail_closed(config, tmp_path):
    from jellyorganize.filesystem.transaction import Transaction
    store = PlanStore(tmp_path / 'plans')
    plan = store.create([], config, ('movie',))
    path = store.root / f'{plan.plan_id}.json'
    path.chmod(0o600)
    payload = json.loads(path.read_text())
    payload['version'] = 99
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='version mismatch'):
        store.load(plan.plan_id)
    transaction = Transaction(tmp_path / 'transactions', plan.plan_id)
    transaction.data['version'] = 99
    transaction.write()
    with pytest.raises(ValueError, match='unsupported'):
        Transaction.load(tmp_path / 'transactions', transaction.data['transaction_id'])


def test_scheduled_recovery_still_emits_one_persisted_json_summary(tmp_path, monkeypatch, capsys):
    path, roots = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, 'recover', lambda config: ({'RECOVERED': 1, 'BLOCKED': 0}, []))
    assert cli.main(['--config', str(path), 'run']) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['recovered_items'] == 1
    operations = Operations(load_config(path).state_dir / 'operations.sqlite3')
    assert operations.status()['last_run']['summary']['recovered_items'] == 1


@pytest.mark.parametrize('changed', [False, True])
def test_manual_organize_observes_stability_in_one_invocation(tmp_path, monkeypatch, capsys, changed):
    import time
    path, roots = setup(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace('stability_seconds = 0', 'stability_seconds = 60'))
    source = roots['movie_in'] / 'We.Live.in.Time.2024.mkv'
    source.write_bytes(b'completed movie')
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    delays = []
    async def observe(delay):
        delays.append(delay)
        clock[0] += delay
        if changed:
            source.write_bytes(b'downloader still writing')
    monkeypatch.setattr(cli.asyncio, 'sleep', observe)
    assert cli.main(['--config', str(path), 'organize', 'movies']) == 0
    output = capsys.readouterr().out
    assert delays == [60]
    assert 'rescanning Incoming' in output
    if changed:
        assert source.read_bytes() == b'downloader still writing'
        assert not list(roots['movie_lib'].rglob('*.mkv'))
    else:
        assert not source.exists() and 'APPLIED: 1' in output
