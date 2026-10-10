import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest

from jellyorganize.downloads.handoff import import_torrent, file_states
from jellyorganize.downloads.qbittorrent import QBittorrentClient, DownloadError, stopped_complete
from jellyorganize.filesystem.undo import undo_transaction
from jellyorganize.config import QBittorrent

HASH = 'a' * 40


class TorrentClient:
    def __init__(self, config, files, state='stoppedUP'):
        self.config, self.files, self.state = config, files, state
        self.calls = 0
    def assert_no_active_overlap(self, torrent_id, paths, root, *, allow_seeding=False):
        assert torrent_id == HASH
    def torrent(self, torrent_id, *, include_files=False):
        assert torrent_id == HASH
        self.calls += 1
        return {'hash': HASH, 'progress': 1, 'amount_left': 0, 'state': self.state,
                'save_path': str(self.config.downloads.path),
                'content_path': str(self.config.downloads.path / 'The Bear (2022)')}, self.files if include_files else []


def prepare(config, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    config.downloads.path = tmp_path / 'downloads'
    config.incoming.path = tmp_path / 'Incoming'
    if os.environ.get('JELLYORGANIZE_TEST_HANDOFF_PARENT'):
        config.incoming.path = Path(tempfile.mkdtemp(prefix='.jo-handoff-test-', dir=os.environ['JELLYORGANIZE_TEST_HANDOFF_PARENT'])) / 'Incoming'
    config.qbittorrent.url = 'http://localhost:8090/qbittorrent'
    config.incoming.path.mkdir(parents=True)
    files = []
    for name, content in [('Season 2/The.Bear.S02E03.mkv', b'episode'),
                          ('Season 2/The.Bear.S02E03.en.srt', b'subtitle'),
                          ('Season 1/The.Bear.S01E01.mkv', b'pilot'), ('notes.txt', b'release notes')]:
        relative = Path('The Bear (2022)') / name
        source = config.downloads.path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        files.append({'name': str(relative), 'size': len(content), 'progress': 1, 'priority': 1})
    return TorrentClient(config, files)


def test_stopped_torrent_handoff_preserves_nested_tree_and_is_idempotent(config, tmp_path, monkeypatch):
    client = prepare(config, tmp_path, monkeypatch)
    plan, result = import_torrent(config, HASH, client=client)
    assert result['status'] == 'handed off'
    target = config.incoming.path / 'The Bear (2022)' / 'Season 2' / 'The.Bear.S02E03.mkv'
    assert target.read_bytes() == b'episode'
    assert target.with_suffix('.en.srt').read_bytes() == b'subtitle'
    assert not (config.downloads.path / 'The Bear (2022)' / 'Season 2' / target.name).exists()
    assert client.calls == 2  # Snapshot and current state immediately before moves.
    target.unlink()  # Simulate subsequent successful organization.
    _, repeated = import_torrent(config, HASH, client=client)
    assert repeated['status'] == 'already handed off' and client.calls == 2
    assert not target.exists()


@pytest.mark.parametrize('state', ['uploading', 'stalledUP', 'queuedUP', 'checkingUP', 'downloading', 'stoppedDL', 'missingFiles'])
def test_active_or_incomplete_torrent_is_never_moved(config, tmp_path, monkeypatch, state):
    client = prepare(config, tmp_path, monkeypatch)
    client.state = state
    with pytest.raises(DownloadError, match='fully downloaded and stopped'):
        import_torrent(config, HASH, client=client)
    assert not list(config.incoming.path.rglob('*.mkv'))
    assert len(list(config.downloads.path.rglob('*.mkv'))) == 2


def test_dry_run_and_collision_leave_all_files_untouched(config, tmp_path, monkeypatch):
    client = prepare(config, tmp_path, monkeypatch)
    plan, result = import_torrent(config, HASH, dry_run=True, client=client)
    assert result['status'] == 'dry run; untouched' and plan.workflow == 'handoff'
    assert len(list(config.downloads.path.rglob('*.mkv'))) == 2
    occupied = config.incoming.path / Path(client.files[0]['name'])
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        import_torrent(config, HASH, client=client)
    assert occupied.read_bytes() == b'existing'
    assert len(list(config.downloads.path.rglob('*.mkv'))) == 2


def test_handoff_undo_restores_original_paths(config, tmp_path, monkeypatch):
    client = prepare(config, tmp_path, monkeypatch)
    _, result = import_torrent(config, HASH, client=client)
    _, counts = undo_transaction(result['transaction_id'], config)
    assert counts['UNDONE'] == 1
    assert len(list(config.downloads.path.rglob('*.mkv'))) == 2
    assert not list(config.incoming.path.rglob('*.mkv'))


@pytest.mark.parametrize('change', ['outside', 'traversal', 'symlink', 'size', 'partial', 'resumed'])
def test_handoff_rejects_unsafe_or_changed_files(config, tmp_path, monkeypatch, change):
    client = prepare(config, tmp_path, monkeypatch)
    if change == 'outside':
        client.files[0]['name'] = '/etc/passwd'
    elif change == 'traversal':
        client.files[0]['name'] = '../escape.mkv'
    elif change == 'symlink':
        folder = config.downloads.path / 'The Bear (2022)' / 'Season 2'
        folder.rename(config.downloads.path / 'original-season')
        folder.symlink_to(config.downloads.path / 'original-season', target_is_directory=True)
    elif change == 'size':
        client.files[0]['size'] += 1
    elif change == 'partial':
        client.files[0]['progress'] = 0.99
    elif change == 'resumed':
        original = client.torrent
        def resume(torrent_id, *, include_files=False):
            if client.calls:
                client.state = 'uploading'
            return original(torrent_id, include_files=include_files)
        client.torrent = resume
    with pytest.raises((ValueError, OSError)):
        import_torrent(config, HASH, client=client)
    assert not list(config.incoming.path.rglob('*.mkv'))


def test_qbittorrent_session_uses_proxy_base_path_and_never_changes_torrents(monkeypatch):
    requests = []
    monkeypatch.setenv('QBITTORRENT_PASSWORD', 'synthetic-secret')
    def handle(request):
        requests.append((request.method, request.url.path))
        assert request.headers['referer'] == 'https://example.test/qbittorrent/'
        if request.url.path.endswith('auth/login'):
            assert b'password=synthetic-secret' in request.content
            return httpx.Response(200, text='Ok.', headers={'set-cookie': 'SID=synthetic; Path=/qbittorrent/'})
        assert request.headers['cookie'] == 'SID=synthetic'
        if request.url.path.endswith('torrents/info'):
            return httpx.Response(200, json=[{'hash': HASH}])
        return httpx.Response(200, json=[])
    settings = QBittorrent(url='https://example.test/qbittorrent', username='example')
    client = QBittorrentClient(settings, transport=httpx.MockTransport(handle))
    torrent, files = client.torrent(HASH, include_files=True)
    assert torrent['hash'] == HASH and files == []
    assert requests == [('POST', '/qbittorrent/api/v2/auth/login'), ('GET', '/qbittorrent/api/v2/torrents/info'),
                        ('GET', '/qbittorrent/api/v2/torrents/files')]


def test_unselected_files_remain_in_downloads(config, tmp_path, monkeypatch):
    client = prepare(config, tmp_path, monkeypatch)
    client.files[0]['priority'] = 0
    client.files[0]['progress'] = 0.1
    source = config.downloads.path / client.files[0]['name']
    import_torrent(config, HASH, client=client)
    assert source.read_bytes() == b'episode'
    assert not (config.incoming.path / client.files[0]['name']).exists()


def test_source_on_another_canonical_home_path_maps_without_following_children(config, tmp_path, monkeypatch):
    from jellyorganize.downloads.handoff import local_path
    actual = tmp_path / 'actual-home'
    actual.mkdir()
    logical = tmp_path / 'home'
    logical.symlink_to(actual, target_is_directory=True)
    root = logical / 'downloads'
    root.mkdir()
    assert local_path(actual / 'downloads' / 'nested.mkv', root) == root / 'nested.mkv'
    with pytest.raises(DownloadError):
        local_path(actual / 'other' / 'outside.mkv', root)


def test_credentials_can_be_saved_from_environment_without_systemd(tmp_path, monkeypatch, capsys):
    from jellyorganize import cli
    from test_auto import setup
    path, _ = setup(tmp_path, monkeypatch)
    secret = tmp_path / 'private-token'
    with path.open('a') as stream:
        stream.write(f'[service]\ncredential_file = "{secret}"\n')
    monkeypatch.setenv('TMDB_API_TOKEN', 'synthetic-token-never-real')
    assert cli.main(['--config', str(path), 'credentials', 'tmdb', '--from-env']) == 0
    assert 'synthetic-token-never-real' not in capsys.readouterr().out
    assert secret.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('TMDB_API_TOKEN', 'different-synthetic-token')
    assert cli.main(['--config', str(path), 'credentials', 'tmdb', '--from-env']) == 4
    assert secret.read_text() == 'synthetic-token-never-real'
    assert 'different-synthetic-token' not in capsys.readouterr().err


def test_import_hook_automatically_organizes_nested_files(config, tmp_path, monkeypatch, capsys):
    from jellyorganize import cli
    from jellyorganize.downloads import handoff
    from test_auto import FakeTMDb, FakeTVmaze
    from jellyorganize.config import load_config
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    client = prepare(config, tmp_path, monkeypatch)
    config.movies.library.mkdir(parents=True)
    config.tv.library.mkdir(parents=True)
    path = tmp_path / 'config.toml'
    path.write_text(f'''[incoming]
path = "{config.incoming.path}"
minimum_age_seconds = 0
stability_seconds = 0
[movies]
library = "{config.movies.library}"
[tv]
library = "{config.tv.library}"
[downloads]
path = "{config.downloads.path}"
[qbittorrent]
url = "http://localhost:8090/qbittorrent"
''')
    monkeypatch.setattr(handoff, 'QBittorrentClient', lambda settings: client)
    monkeypatch.setattr(cli, 'TMDbProvider', FakeTMDb)
    monkeypatch.setattr(cli, 'TVmazeProvider', FakeTVmaze)
    assert cli.main(['--config', str(path), 'import-download', '--torrent', HASH]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]['download']['status'] == 'handed off'
    assert output[1]['apply']['APPLIED'] == 2
    assert len(list(config.tv.library.rglob('*.mkv'))) == 2
    assert len(list(config.tv.library.rglob('*.srt'))) == 1
    assert not list(config.downloads.path.rglob('*.mkv'))
    assert (config.incoming.path / 'The Bear (2022)' / 'notes.txt').read_bytes() == b'release notes'
    assert cli.main(['--config', str(path), 'import-download', '--torrent', HASH]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]['download']['status'] == 'already handed off'
    assert not output[1]['apply']


@pytest.mark.parametrize('operation', ['handoff', 'undo'])
@pytest.mark.parametrize('copy', [False, True])
@pytest.mark.parametrize('stage', ['intent_written', 'stage_created', 'stage_recorded', 'stage_verified',
                                  'published', 'source_removed', 'move_recorded', 'item_committed', 'transaction_committed'])
def test_interrupted_handoff_and_undo_recover_nested_files(config, tmp_path, monkeypatch, operation, copy, stage):
    from test_recovery import worker
    from jellyorganize.downloads import handoff
    from jellyorganize.filesystem.recovery import recover
    client = prepare(config, tmp_path, monkeypatch)
    monkeypatch.setattr(handoff, 'QBittorrentClient', lambda settings: client)
    torrent, _ = client.torrent(HASH)
    payload = {'operation': operation, 'copy': copy, 'stage': stage,
               'torrent': torrent, 'download_files': client.files}
    if operation == 'undo':
        _, result = import_torrent(config, HASH, client=client)
        payload['transaction_id'] = result['transaction_id']
    worker(config, tmp_path, payload)
    counts, errors = recover(config)
    assert not errors, errors
    present, absent = ((config.downloads.path, config.incoming.path) if operation == 'undo'
                       else (config.incoming.path, config.downloads.path))
    for row in client.files:
        assert (present / row['name']).stat().st_size == row['size']
        assert not (absent / row['name']).exists()
    assert (present / client.files[0]['name']).read_bytes() == b'episode'
    assert (present / client.files[1]['name']).read_bytes() == b'subtitle'
    assert recover(config) == ({'RECOVERED': 0, 'BLOCKED': 0}, [])
    if operation == 'handoff':
        _, repeated = import_torrent(config, HASH, client=client)
        assert repeated['status'] == 'already handed off'


def test_handoff_recovery_waits_for_resumed_torrent_to_stop(config, tmp_path, monkeypatch):
    from test_recovery import worker
    from jellyorganize.downloads import handoff
    from jellyorganize.filesystem.recovery import recover
    client = prepare(config, tmp_path, monkeypatch)
    monkeypatch.setattr(handoff, 'QBittorrentClient', lambda settings: client)
    torrent, _ = client.torrent(HASH)
    worker(config, tmp_path, {'operation': 'handoff', 'stage': 'source_removed',
                            'torrent': torrent, 'download_files': client.files})
    client.state = 'uploading'
    counts, errors = recover(config)
    assert counts['BLOCKED'] == 1 and errors
    assert len(list(config.downloads.path.rglob('*.mkv'))) >= 1
    client.state = 'missingFiles'  # Permitted only for this interrupted, previously verified handoff.
    assert not recover(config)[1]
    assert len(list(config.incoming.path.rglob('*.mkv'))) == 2


def test_copied_handoff_rollback_can_retry_only_verified_restored_files(config, tmp_path, monkeypatch):
    import errno
    from jellyorganize.filesystem import apply
    client = prepare(config, tmp_path, monkeypatch)
    original_move = apply._move_file
    original_link = os.link
    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, 'force copy')
    def fail_last(file, *args):
        if file.source.name == 'The.Bear.S01E01.mkv':
            raise OSError('injected media failure')
        return original_move(file, *args)
    monkeypatch.setattr(os, 'link', exdev)
    monkeypatch.setattr(apply, '_move_file', fail_last)
    with pytest.raises(DownloadError, match='did not complete'):
        import_torrent(config, HASH, client=client)
    assert len(list(config.downloads.path.rglob('*.mkv'))) == 2
    assert not list(config.incoming.path.rglob('*.mkv'))
    monkeypatch.setattr(os, 'link', original_link)
    monkeypatch.setattr(apply, '_move_file', original_move)
    _, result = import_torrent(config, HASH, client=client)
    assert result['status'] == 'handed off'


def test_proxy_credentials_and_error_responses_are_private(monkeypatch):
    import base64
    monkeypatch.setenv('QBITTORRENT_BASIC_PASSWORD', 'synthetic-proxy-secret')
    def handle(request):
        expected = base64.b64encode(b'example:synthetic-proxy-secret').decode()
        assert request.headers['authorization'] == 'Basic ' + expected
        return httpx.Response(403, text='synthetic-proxy-secret echoed by upstream')
    settings = QBittorrent(url='https://example.test/qbittorrent', basic_username='example')
    with pytest.raises(DownloadError, match='HTTP 403') as failure:
        QBittorrentClient(settings, transport=httpx.MockTransport(handle)).torrent(HASH)
    assert 'synthetic-proxy-secret' not in str(failure.value)


def test_config_check_can_authenticate_without_touching_torrents(tmp_path, monkeypatch, capsys):
    from jellyorganize import cli
    from jellyorganize.downloads import qbittorrent
    from test_auto import setup
    path, _ = setup(tmp_path, monkeypatch)
    with path.open('a') as stream:
        stream.write('[qbittorrent]\nurl = "http://example.test/qbittorrent"\n')
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text='v5.1.0')
    real_client = QBittorrentClient
    monkeypatch.setattr(qbittorrent, 'QBittorrentClient', lambda settings: real_client(settings, transport=httpx.MockTransport(handle)))
    assert cli.main(['--config', str(path), 'config', 'check', '--download-client']) == 0
    assert 'qBittorrent connection: OK (v5.1.0)' in capsys.readouterr().out
    assert calls == [('GET', '/qbittorrent/api/v2/app/version')]


@pytest.mark.parametrize('state,blocked', [('uploading', True), ('stalledUP', True), ('stoppedDL', True),
                                         ('stoppedUP', False)])
def test_another_torrent_sharing_paths_must_also_be_stopped(tmp_path, state, blocked):
    root = tmp_path / 'downloads'
    root.mkdir()
    rows = [{'hash': 'b' * 40, 'state': state, 'progress': 1, 'amount_left': 0,
             'content_path': str(root / 'Shared')}]
    client = QBittorrentClient(QBittorrent(url='http://example.test'),
                               transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows)))
    if blocked:
        with pytest.raises(DownloadError, match='another active or incomplete torrent'):
            client.assert_no_active_overlap(HASH, [root / 'Shared' / 'episode.mkv'], root)
    else:
        client.assert_no_active_overlap(HASH, [root / 'Shared' / 'episode.mkv'], root)
    rows[0]['state'] = 'uploading'
    client.assert_no_active_overlap(HASH, [root / 'Different' / 'episode.mkv'], root)


@pytest.mark.parametrize("state", ["uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP", "stoppedUP"])
def test_hardlink_completed_seeding_handoff_is_idempotent(config, tmp_path, monkeypatch, state):
    client = prepare(config, tmp_path, monkeypatch)
    config.filesystem.mode = "hardlink"
    client.state = state
    plan, result = import_torrent(config, HASH, client=client)
    assert result["status"] == "handed off"
    for file in plan.entries[0].files:
        assert os.path.samefile(file.source, file.destination)
    _, repeated = import_torrent(config, HASH, client=client)
    assert repeated["status"] == "already handed off"


@pytest.mark.parametrize("changes", [{"state": "checkingUP"}, {"state": "moving"},
                                      {"progress": .999}, {"amount_left": 1}, {"state": "downloading"}])
def test_hardlink_does_not_accept_unfinished_torrents(changes):
    torrent = {"state": "uploading", "progress": 1, "amount_left": 0, **changes}
    with pytest.raises(DownloadError):
        stopped_complete(torrent, allow_seeding=True)


def test_overlapping_complete_seeder_allowed_only_for_links(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    content = root / "shared"
    rows = [{"hash": "b" * 40, "content_path": str(content), "state": "uploading", "progress": 1, "amount_left": 0}]
    client = QBittorrentClient(QBittorrent(url="http://localhost:8080"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows)))
    with pytest.raises(DownloadError, match="shares"):
        client.assert_no_active_overlap(HASH, [content / "movie.mkv"], root)
    client.assert_no_active_overlap(HASH, [content / "movie.mkv"], root, allow_seeding=True)
    rows[0]["progress"] = .5
    with pytest.raises(DownloadError, match="shares"):
        client.assert_no_active_overlap(HASH, [content / "movie.mkv"], root, allow_seeding=True)
