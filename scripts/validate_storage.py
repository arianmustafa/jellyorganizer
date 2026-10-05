"""Isolated real-filesystem apply/undo, crash recovery, and repeated-run check.

Uses synthetic media bytes and explicitly labeled identities, without credentials.
Creates fresh scratch directories; never scans an existing library.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from jellyorganize import __version__
from jellyorganize.config import Config
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.filesystem.recovery import recover
from jellyorganize.filesystem.undo import undo_transaction
from jellyorganize.models import Candidate, MediaItem, Proposal
from jellyorganize.naming.jellyfin import destinations
from jellyorganize.planning.store import PlanStore

STAGES = ['intent_written', 'stage_created', 'stage_recorded', 'stage_verified', 'published',
          'source_removed', 'move_recorded', 'item_committed', 'transaction_committed']


def worker(payload):
    from jellyorganize.filesystem import apply, durable, undo
    config = Config.model_validate(payload['config'])
    def crash(stage):
        if stage == payload['stage']:
            os._exit(73)
    durable.checkpoint = apply.checkpoint = undo.checkpoint = crash
    if payload['operation'] == 'undo':
        undo_transaction(payload['transaction_id'], config)
    elif payload['operation'] == 'recover':
        recover(config)
    else:
        plan = PlanStore(config.state_dir / 'plans').load(payload['plan_id'])
        apply_plan(plan, config, config.state_dir / 'transactions')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library-parent', type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument('--cycles', type=int, default=25)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(json.loads(args.worker.read_text()))
        return
    if not 1 <= args.cycles <= 1000:
        parser.error('cycles must be between 1 and 1000')
    scratch = Path(tempfile.mkdtemp(prefix='jellyorganize-validate-'))
    library = Path(tempfile.mkdtemp(prefix='.jellyorganize-validate-', dir=args.library_parent))
    os.environ.update(XDG_STATE_HOME=str(scratch / 'state'), XDG_CACHE_HOME=str(scratch / 'cache'),
                      XDG_DATA_HOME=str(scratch / 'data'))
    media = b'synthetic-media-data' * 256000
    subtitle = b'synthetic subtitle\n'
    checks = []
    def prepare(index):
        root = scratch / str(index)
        config = Config.model_validate({
            'incoming': {'path': root / 'Incoming', 'minimum_age_seconds': 0, 'stability_seconds': 0},
            'movies': {'library': library / str(index) / 'Movies'},
            'tv': {'library': library / str(index) / 'TV'},
        })
        source = config.incoming.path / 'Dune.2021.mkv'
        source.parent.mkdir(parents=True)
        config.movies.library.mkdir(parents=True)
        config.tv.library.mkdir(parents=True)
        source.write_bytes(media)
        sidecar = source.with_suffix('.en.srt')
        sidecar.write_bytes(subtitle)
        item = MediaItem(source, 'movie', config.incoming.path, [sidecar])
        candidate = Candidate(provider='tmdb', provider_id='438631', kind='movie', title='Dune', year=2021)
        target, sidecars = destinations(item, candidate, config)
        proposal = Proposal(item, 'CONFIRMED', 'validation fixture identity', 1.0, candidate, target, sidecars)
        plan = PlanStore(config.state_dir / 'plans').create([proposal], config)
        return config, source, sidecar, target, plan
    def verify(source, target, in_library):
        present, absent = (target, source) if in_library else (source, target)
        assert hashlib.sha256(present.read_bytes()).digest() == hashlib.sha256(media).digest()
        assert present.with_suffix('.en.srt').read_bytes() == subtitle
        assert not absent.exists() and not absent.with_suffix('.en.srt').exists()
    for operation in ['apply', 'undo']:
        for stage in STAGES:
            config, source, sidecar, target, plan = prepare(len(checks))
            payload = {'config': config.model_dump(mode='json'), 'operation': operation,
                       'stage': stage, 'plan_id': plan.plan_id}
            if operation == 'undo':
                transaction, counts = apply_plan(plan, config, config.state_dir / 'transactions')
                assert counts['APPLIED'] == 1
                payload['transaction_id'] = transaction.data['transaction_id']
            file = scratch / 'worker.json'
            file.write_text(json.dumps(payload))
            killed = subprocess.run([sys.executable, str(Path(__file__).absolute()), '--worker', str(file)],
                                    capture_output=True, text=True, timeout=60)
            assert killed.returncode == 73, killed.stderr
            _, errors = recover(config)
            assert not errors, errors
            verify(source, target, operation == 'apply')
            assert recover(config) == ({'RECOVERED': 0, 'BLOCKED': 0}, [])
            checks.append(f'{operation}:{stage}')
    for cycle in range(args.cycles):
        config, source, sidecar, target, plan = prepare(len(checks) + cycle)
        transaction, counts = apply_plan(plan, config, config.state_dir / 'transactions')
        assert counts['APPLIED'] == 1
        verify(source, target, True)
        _, repeated = apply_plan(plan, config, config.state_dir / 'transactions')
        assert repeated['APPLIED'] == 0
        _, counts = undo_transaction(transaction.data['transaction_id'], config)
        assert counts['UNDONE'] == 1
        verify(source, target, False)
        _, counts = undo_transaction(transaction.data['transaction_id'], config)
        assert counts['UNDONE'] == 0
    result = {'version': __version__, 'passed': True, 'crash_checks': len(checks),
              'repeat_cycles': args.cycles, 'source_device': scratch.stat().st_dev,
              'destination_device': library.stat().st_dev,
              'cross_filesystem': scratch.stat().st_dev != library.stat().st_dev,
              'scratch': str(scratch), 'library_scratch': str(library)}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
