"""Bounded unattended Incoming soak with synthetic files and frozen metadata.

Creates fresh isolated roots and retains a status report. No real providers or
existing media are accessed. One cycle exercises completion, automatic matching,
exception deduplication, persistent health, and undo through the public CLI.
"""
import argparse
import contextlib
import io
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from jellyorganize import cli
from jellyorganize.benchmark import load_corpus, SnapshotTMDb
from jellyorganize.config import load_config
from jellyorganize.operations import Operations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--cycles', type=int, help='finite quick validation instead of timed soak')
    args = parser.parse_args()
    if args.interval < 0 or args.hours <= 0 or (args.cycles is not None and args.cycles < 1):
        parser.error('duration/cycles must be positive and interval nonnegative')
    if args.cycles is None and args.interval == 0:
        parser.error('timed soak requires a positive interval')
    root = args.root.absolute() if args.root else Path(tempfile.mkdtemp(prefix='jellyorganize-soak-'))
    if args.root:
        root.mkdir(mode=0o700)  # Refuse to reuse any existing directory.
    for directory in ('Incoming', 'Movies', 'TV'):
        (root / directory).mkdir()
    os.environ.update(XDG_STATE_HOME=str(root / 'state'), XDG_DATA_HOME=str(root / 'data'),
                      XDG_CACHE_HOME=str(root / 'cache'))
    # There are no live provider requests or credential files in this soak.
    os.environ.pop('TMDB_API_TOKEN', None)
    path = root / 'config.toml'
    path.write_text(f'''schema_version = 1
[incoming]
path = "{root / 'Incoming'}"
completion = "marker"
minimum_age_seconds = 0
[movies]
library = "{root / 'Movies'}"
[tv]
library = "{root / 'TV'}"
[providers]
tvmaze = false
[service]
credential_file = "{root / 'unused-token'}"
''')
    snapshots = load_corpus()['snapshots']
    class FrozenTMDb(SnapshotTMDb):
        def __init__(self, *arguments):
            super().__init__(snapshots['dune'])
        async def search_movie(self, title, year=None):
            return await SnapshotTMDb(snapshots['arrival'] if title.casefold() == 'arrival' else snapshots['dune']).search_movie(title, year)
    cli.TMDbProvider = FrozenTMDb
    config = load_config(path)
    operations = Operations(config.state_dir / 'operations.sqlite3')
    positive = root / 'Incoming' / 'Dune.2021.mkv'
    negative = root / 'Incoming' / 'Arrival.2016.mkv'
    negative.write_bytes(b'synthetic ambiguous release')
    def invoke(*arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['--config', str(path), *arguments])
        return code, output.getvalue()
    assert invoke('ready', str(negative))[0] == 0
    cycles = args.cycles or math.ceil(args.hours * 3600 / args.interval)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'completed_cycles': 0,
              'planned_cycles': cycles, 'state': 'running', 'scratch': str(root)}
    def save():
        temporary = root / 'status.tmp'
        temporary.write_text(json.dumps(report, indent=2) + '\n')
        temporary.replace(root / 'status.json')
    save()
    try:
        for index in range(cycles):
            started = time.monotonic()
            media = b'synthetic completed movie ' + str(index).encode()
            positive.write_bytes(media)
            code, output = invoke('run', 'movies')
            summary = json.loads(output)
            assert code == 2 and summary['counts']['SKIP'] == 1 and positive.exists()
            assert invoke('ready', str(positive))[0] == 0
            code, output = invoke('run', 'movies')
            summary = json.loads(output)
            assert code == 2 and summary['apply']['APPLIED'] == 1 and not positive.exists()
            assert summary['changed_exceptions'] == 0
            assert negative.read_bytes() == b'synthetic ambiguous release'
            assert len(operations.exceptions()) == 1
            assert operations.status()['last_run']['exit_code'] == 2
            transaction = json.loads((config.state_dir / 'transactions' / (summary['transaction_id'] + '.json')).read_text())
            destination = Path(transaction['items'][0]['destination'])
            assert destination.read_bytes() == media
            assert invoke('undo', transaction['transaction_id'])[0] == 0
            assert positive.read_bytes() == media and not destination.exists()
            report.update(completed_cycles=index + 1, last_success_at=datetime.now(timezone.utc).isoformat())
            save()
            if index + 1 < cycles:
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
        report.update(state='passed', completed_at=datetime.now(timezone.utc).isoformat())
        save()
    except BaseException as error:
        report.update(state='failed', error=f'{type(error).__name__}: {error}')
        save()
        raise
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
