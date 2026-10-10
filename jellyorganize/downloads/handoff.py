"""Recoverable, idempotent moves of stopped torrents into configured Incoming."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from jellyorganize.completion import CompletionTracker
from jellyorganize.downloads.qbittorrent import DownloadError, QBittorrentClient, stopped_complete
from jellyorganize.filesystem.apply import _apply_plan, _validate_entry
from jellyorganize.filesystem.locking import media_lock
from jellyorganize.filesystem.recovery import recover_pending
from jellyorganize.filesystem.safety import relative_file
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.planning.store import PlanStore, FileState, capture, configured_roots
from jellyorganize.scanner.incoming import scan_incoming
from jellyorganize.scanner.sidecars import MEDIA_EXTENSIONS


def validate_handoff(plan, config):
    from jellyorganize.filesystem.links import LinkedImports
    _validate_entry(plan.entries[0], config, 'handoff', mode=plan.transfer_mode, links=LinkedImports(config))


def torrent_hash(value):
    if not re.fullmatch(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})', value):
        raise DownloadError('torrent ID must be a 40- or 64-character hexadecimal hash')
    return value.lower()


def verify_torrent(plan, config, *, recovering=False, client=None):
    if plan.downloader_url != config.qbittorrent.url or not plan.download_hash or not plan.download_source:
        raise DownloadError('handoff downloader identity no longer matches configuration')
    client = client or QBittorrentClient(config.qbittorrent)
    torrent, _ = client.torrent(torrent_hash(plan.download_hash))
    stopped_complete(torrent, recovering=recovering)
    if Path(torrent.get('content_path', '')) != plan.download_source:
        raise DownloadError('torrent content path changed after handoff was planned')
    client.assert_no_active_overlap(plan.download_hash, [file.source for entry in plan.entries for file in entry.files],
                                    config.downloads.path)


def local_path(value, root):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise DownloadError('qBittorrent returned an unsafe content or file path')
    # Accept an API path beneath the canonical root when only a configured
    # ancestor (such as /home) is symlinked. Never resolve links below the root.
    for prefix in (root, root.resolve(strict=True)):
        try:
            relative = path.relative_to(prefix)
        except ValueError:
            continue
        return root / relative
    raise DownloadError('torrent content is outside configured downloads.path')


def file_states(torrent, files, config):
    source_root, destination_root = configured_roots(config, 'download', 'handoff')
    content = local_path(torrent.get('content_path', ''), source_root)
    save = local_path(torrent.get('save_path', ''), source_root)
    states = []
    for row in files:
        priority = row.get('priority')
        if type(priority) is not int or priority < 0:
            raise DownloadError('qBittorrent returned malformed file priority')
        if priority == 0:
            continue  # Do not move unselected or partly downloaded files.
        name = row.get('name')
        if not isinstance(name, str) or not name or '\x00' in name:
            raise DownloadError('qBittorrent returned an invalid file name')
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise DownloadError('qBittorrent file name escapes its save directory')
        source = save / relative
        if source != content and content not in source.parents:
            raise DownloadError('torrent file is outside its declared content path')
        if type(row.get('progress')) not in (int, float) or row['progress'] != 1:
            raise DownloadError('a selected torrent file is not completely downloaded')
        if source.suffix.lower() in {suffix.lower() for suffix in config.incoming.ignored_extensions}:
            raise DownloadError('torrent still contains temporary files')
        state = capture(source, destination_root / relative_file(source, source_root))
        if type(row.get('size')) is not int or row['size'] != state.size:
            raise DownloadError('torrent file size does not match the client manifest')
        states.append(state)
    states.sort(key=lambda state: (state.source.suffix.lower() not in MEDIA_EXTENSIONS, str(state.source)))
    if not states or not any(state.source.suffix.lower() in MEDIA_EXTENSIONS for state in states):
        return []
    return states


def acknowledge_handoff(plan, config):
    fast = config.model_copy(deep=True)
    fast.incoming.minimum_age_seconds = 0
    transferred = {state.destination for entry in plan.entries for state in entry.files}
    tracker = CompletionTracker(config.state_dir / 'completion.sqlite3')
    for kind in ('movie', 'tv'):
        for item in scan_incoming(fast, kind).items:
            if item.path in transferred and not item.reason:
                tracker.acknowledge(item)


def retry_states(plan, journal, states):
    """Only refresh an inode when a completed rollback proves its replacement."""
    original = {str(state.source): state for entry in plan.entries for state in entry.files}
    if set(original) != {str(state.source) for state in states}:
        raise DownloadError('torrent file selection changed after the previous handoff')
    receipts = {move['source_state']['source']: move
                for item in journal.data['items'] for move in item.get('moves', [])}
    for current in states:
        expected = original[str(current.source)]
        receipt = receipts.get(str(current.source), {})
        rollback = receipt.get('rollback', {})
        if rollback:
            if (receipt.get('source_state') != expected.model_dump(mode='json') or
                    receipt.get('phase') != 'rolled_back' or rollback.get('phase') != 'moved' or
                    rollback.get('source_state') != receipt.get('destination_state')):
                raise DownloadError('previous handoff rollback is not complete')
            restored = FileState.model_validate(rollback['destination_state'])
            if (restored.source != expected.source or restored.destination != expected.destination or
                    restored.size != expected.size or restored.mtime_ns != expected.mtime_ns):
                raise DownloadError('previous handoff rollback changed file decisions')
            expected = restored
        if current != expected:
            raise DownloadError('download changed after the previous handoff')
    return states


def import_torrent(config, torrent_id, *, dry_run=False, client=None):
    torrent_id = torrent_hash(torrent_id)
    configured_roots(config, 'download', 'handoff')
    client = client or QBittorrentClient(config.qbittorrent)
    root = config.state_dir / 'transactions'
    store = PlanStore(config.state_dir / 'plans')
    with media_lock(root):
        if not dry_run:
            recover_pending(config, root, fail_on_blocked=True)
        with closing(sqlite3.connect(config.state_dir / 'downloads.sqlite3')) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS handoffs (
                client TEXT NOT NULL, hash TEXT NOT NULL, plan_id TEXT NOT NULL,
                transaction_id TEXT, status TEXT NOT NULL, PRIMARY KEY(client, hash))''')
            row = connection.execute('SELECT plan_id,transaction_id,status FROM handoffs WHERE client=? AND hash=?',
                                     (config.qbittorrent.url, torrent_id)).fetchone()
            if row:
                plan = store.load(row[0])
                expected_source, expected_destination = configured_roots(config, 'download', 'handoff')
                if plan.source_roots != {'download': expected_source} or plan.destination_roots != {'download': expected_destination}:
                    raise DownloadError('previous handoff roots no longer match configuration')
                if plan.transfer_mode == 'hardlink':
                    torrent, files = client.torrent(torrent_id, include_files=True)
                    if Path(torrent.get('content_path', '')) != plan.download_source:
                        raise DownloadError('torrent content path changed after handoff was planned')
                    current = {state.source: state for state in file_states(torrent, files, config)}
                    expected = {state.source: state for entry in plan.entries for state in entry.files}
                    if set(current) != set(expected):
                        raise DownloadError('torrent file selection changed after the previous handoff')
                    if current != expected:
                        raise DownloadError('download changed after the previous handoff')
                complete = row[2] == 'handed_off'
                transaction_id = row[1]
                attempted = None
                if complete and transaction_id:
                    completed = Transaction.load(root, transaction_id)
                    if any(item.get('status') != 'applied' for item in completed.data['items']):
                        complete, attempted = False, completed
                    elif plan.transfer_mode == 'hardlink':
                        from jellyorganize.filesystem.apply import _check_unchanged
                        from jellyorganize.filesystem.links import LinkedImports
                        links = LinkedImports(config)
                        for entry in plan.entries:
                            for file in entry.files:
                                _check_unchanged(file, entry.source_root)
                                if links.owned(file, entry.source_root, entry.destination_root) is None:
                                    complete, attempted = False, completed
                # A process can die after committing moves but before the index.
                if not complete:
                    for path in sorted(root.glob('*.json')):
                        journal = Transaction.load(root, path.stem)
                        data = journal.data
                        if data['plan_id'] != plan.plan_id or data.get('operation') == 'undo':
                            continue
                        attempted = journal
                        if data['items'] and all(item.get('status') == 'applied' for item in data['items']):
                            if plan.transfer_mode == 'hardlink':
                                from jellyorganize.filesystem.links import LinkedImports
                                links = LinkedImports(config)
                                if not all(links.owned(file, entry.source_root, entry.destination_root) is not None
                                           for entry in plan.entries for file in entry.files):
                                    continue
                            complete, transaction_id = True, data['transaction_id']
                            break
                if complete:
                    if not dry_run:
                        connection.execute('UPDATE handoffs SET status=?,transaction_id=? WHERE client=? AND hash=?',
                                           ('handed_off', transaction_id, config.qbittorrent.url, torrent_id))
                        acknowledge_handoff(plan, config)
                    return plan, {'status': 'already handed off', 'transaction_id': transaction_id}
                if attempted and not dry_run:
                    verify_torrent(plan, config, client=client)
                    torrent, files = client.torrent(torrent_id, include_files=True)
                    stopped_complete(torrent)
                    states = retry_states(plan, attempted, file_states(torrent, files, config))
                    frozen = config.model_copy(deep=True)
                    frozen.filesystem.mode = plan.transfer_mode
                    plan = store.create_handoff(states, frozen, torrent_id, config.qbittorrent.url, plan.download_source)
                    validate_handoff(plan, config)
                    connection.execute('UPDATE handoffs SET plan_id=? WHERE client=? AND hash=?',
                                       (plan.plan_id, config.qbittorrent.url, torrent_id))
                    connection.commit()
            else:
                torrent, files = client.torrent(torrent_id, include_files=True)
                stopped_complete(torrent)
                states = file_states(torrent, files, config)
                if not states:
                    return None, {'status': 'no selected media files; untouched', 'transaction_id': None}
                plan = store.create_handoff(states, config, torrent_id, config.qbittorrent.url, Path(torrent['content_path']))
                validate_handoff(plan, config)
                if dry_run:
                    verify_torrent(plan, config, client=client)
                    return plan, {'status': 'dry run; untouched', 'transaction_id': None}
                connection.execute('INSERT INTO handoffs VALUES (?, ?, ?, NULL, ?)',
                                   (config.qbittorrent.url, torrent_id, plan.plan_id, 'planned'))
                connection.commit()  # Index exact decisions before any file moves.
            if dry_run:
                validate_handoff(plan, config)
                verify_torrent(plan, config, client=client)
                return plan, {'status': 'dry run; untouched', 'transaction_id': None}
            verify_torrent(plan, config, client=client)
            transaction, counts = _apply_plan(plan, config, root)
            if counts['APPLIED'] != 1:
                raise DownloadError('download handoff did not complete: ' + json.dumps(counts))
            connection.execute('UPDATE handoffs SET status=?,transaction_id=? WHERE client=? AND hash=?',
                               ('handed_off', transaction.data['transaction_id'], config.qbittorrent.url, torrent_id))
            acknowledge_handoff(plan, config)
            return plan, {'status': 'handed off', 'transaction_id': transaction.data['transaction_id']}
