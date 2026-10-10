"""Read-only qBittorrent Web API access. Torrent entries are never removed."""

import os
import stat
import time
from contextlib import contextmanager

import httpx


class DownloadError(ValueError):
    pass


def private_password(path, environment):
    value = os.environ.get(environment, '')
    if value:
        return value
    if path is None:
        return ''
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ''
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077 or current.st_uid != os.getuid():
            raise DownloadError('download-client password file must be owned by the current user and private (chmod 600)')
        return stream.read().strip()


class QBittorrentClient:
    def __init__(self, settings, *, transport=None):
        if not settings.url:
            raise DownloadError('configure qbittorrent.url before importing a torrent')
        self.settings = settings
        self.transport = transport

    @contextmanager
    def session(self):
        basic = None
        if self.settings.basic_username:
            password = private_password(self.settings.basic_password_file, 'QBITTORRENT_BASIC_PASSWORD')
            if not password:
                raise DownloadError('qBittorrent reverse-proxy password is missing')
            basic = httpx.BasicAuth(self.settings.basic_username, password)
        with httpx.Client(base_url=self.settings.url + '/', timeout=10, follow_redirects=False,
                          auth=basic, transport=self.transport,
                          headers={'Referer': self.settings.url + '/'}) as client:
            if self.settings.username:
                password = private_password(self.settings.password_file, 'QBITTORRENT_PASSWORD')
                if not password:
                    raise DownloadError('qBittorrent password is missing; configure a private password_file or QBITTORRENT_PASSWORD')
                response = self.request(client, 'POST', 'auth/login', data={'username': self.settings.username, 'password': password})
                if response.text.strip() != 'Ok.':
                    raise DownloadError('qBittorrent authentication failed')
            yield client

    @staticmethod
    def request(client, method, endpoint, **arguments):
        for attempt in range(3):
            try:
                response = client.request(method, 'api/v2/' + endpoint, **arguments)
            except httpx.TransportError as error:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise DownloadError(f'qBittorrent connection failed: {type(error).__name__}') from None
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise DownloadError(f'qBittorrent request failed: HTTP {response.status_code}')
            return response
        raise DownloadError('qBittorrent request failed')

    def torrent(self, torrent_id, *, include_files=False):
        with self.session() as client:
            response = self.request(client, 'GET', 'torrents/info', params={'hashes': torrent_id})
            try:
                rows = response.json()
            except ValueError:
                raise DownloadError('malformed qBittorrent torrent response') from None
            if not isinstance(rows, list):
                raise DownloadError('malformed qBittorrent torrent response')
            if not rows:
                return None, []
            if len(rows) != 1 or not isinstance(rows[0], dict) or str(rows[0].get('hash', '')).lower() != torrent_id:
                raise DownloadError('qBittorrent returned a different or ambiguous torrent identity')
            files = []
            if include_files:
                response = self.request(client, 'GET', 'torrents/files', params={'hash': torrent_id})
                try:
                    files = response.json()
                except ValueError:
                    raise DownloadError('malformed qBittorrent file response') from None
                if not isinstance(files, list) or not all(isinstance(row, dict) for row in files):
                    raise DownloadError('malformed qBittorrent file response')
            return rows[0], files

    def assert_no_active_overlap(self, torrent_id, paths, root, *, allow_seeding=False):
        """Shared paths may be read by seeders only when originals are retained."""
        from jellyorganize.downloads.handoff import local_path
        with self.session() as client:
            response = self.request(client, 'GET', 'torrents/info')
            try:
                rows = response.json()
            except ValueError:
                raise DownloadError('malformed qBittorrent torrent list') from None
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise DownloadError('malformed qBittorrent torrent list')
            for row in rows:
                if str(row.get('hash', '')).lower() == torrent_id:
                    continue
                if not isinstance(row.get('content_path'), str) or not row['content_path']:
                    raise DownloadError('cannot verify other torrents have separate content paths')
                try:
                    content = local_path(row['content_path'], root)
                except DownloadError:
                    continue
                if any(path == content or content in path.parents for path in paths):
                    try:
                        stopped_complete(row, allow_seeding=allow_seeding)
                    except DownloadError:
                        raise DownloadError('another active or incomplete torrent shares the handoff paths; files are untouched') from None


def stopped_complete(torrent, *, recovering=False, allow_seeding=False):
    if torrent is None:
        raise DownloadError('torrent is absent from qBittorrent; the stopped entry must be retained')
    states = {'pausedUP', 'stoppedUP'}
    if allow_seeding:
        states.update({'uploading', 'stalledUP', 'queuedUP', 'forcedUP'})
    # A partially moved, previously stopped torrent can be reported missing.
    # This state is permitted only while resuming a journal with moved files.
    if recovering:
        states.add('missingFiles')
    if (torrent.get('state') not in states or type(torrent.get('progress')) not in (int, float) or
            torrent['progress'] != 1 or type(torrent.get('amount_left')) is not int or torrent['amount_left'] != 0):
        if allow_seeding:
            raise DownloadError('torrent must be fully downloaded and stopped or seeding; checking and incomplete torrents are untouched')
        raise DownloadError('torrent must be fully downloaded and stopped after seeding; active, checking, and incomplete torrents are untouched')
