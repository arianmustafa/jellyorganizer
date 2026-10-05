"""Run with the old installed wheel, then the new wheel, in isolated XDG dirs."""
import argparse
import json
import os
from pathlib import Path

from jellyorganize import __version__
from jellyorganize.config import Config
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.models import Candidate, MediaItem, Proposal
from jellyorganize.naming.jellyfin import destinations
from jellyorganize.planning.store import PlanStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'verify'])
    parser.add_argument('scratch', type=Path)
    args = parser.parse_args()
    root = args.scratch.absolute()
    os.environ.update(XDG_STATE_HOME=str(root / 'state'), XDG_DATA_HOME=str(root / 'data'),
                      XDG_CACHE_HOME=str(root / 'cache'))
    config = Config.model_validate({
        'movies': {'incoming': root / 'Incoming' / 'Movies', 'library': root / 'Movies'},
        'tv': {'incoming': root / 'Incoming' / 'TV', 'library': root / 'TV'},
        'incoming': {'minimum_age_seconds': 0},
    })
    store = PlanStore(config.state_dir / 'plans')
    manifest = root / 'upgrade.json'
    if args.phase == 'prepare':
        if root.exists():
            raise ValueError('upgrade scratch must be a new directory')
        config.movies.incoming.mkdir(parents=True)
        config.movies.library.mkdir(parents=True)
        records = []
        for title, identity, year in [('Dune', '438631', 2021), ('We Live in Time', '1100099', 2024)]:
            source = config.movies.incoming / f'{title}.{year}.mkv'
            source.write_bytes(b'upgrade-media')
            sidecar = source.with_suffix('.en.srt')
            sidecar.write_bytes(b'upgrade-subtitle')
            item = MediaItem(source, 'movie', config.movies.incoming, [sidecar])
            candidate = Candidate(provider='tmdb', provider_id=identity, kind='movie', title=title, year=year)
            target, sidecars = destinations(item, candidate, config)
            plan = store.create([Proposal(item, 'CONFIRMED', 'upgrade fixture', 1.0, candidate, target, sidecars)], config)
            record = {'plan_id': plan.plan_id, 'source': str(source), 'target': str(target)}
            if not records:
                transaction, counts = apply_plan(plan, config, config.state_dir / 'transactions')
                assert counts['APPLIED'] == 1
                record['transaction_id'] = transaction.data['transaction_id']
            records.append(record)
        manifest.write_text(json.dumps({'old_version': __version__, 'records': records}))
        print(f'Prepared upgrade fixtures with {__version__}')
    else:
        from jellyorganize.filesystem.undo import undo_transaction
        data = json.loads(manifest.read_text())
        first, second = data['records']
        _, counts = undo_transaction(first['transaction_id'], config)
        assert counts['UNDONE'] == 1
        assert Path(first['source']).read_bytes() == b'upgrade-media'
        assert Path(first['source']).with_suffix('.en.srt').read_bytes() == b'upgrade-subtitle'
        plan = store.load(second['plan_id'])
        _, counts = apply_plan(plan, config, config.state_dir / 'transactions')
        assert counts['APPLIED'] == 1
        assert Path(second['target']).read_bytes() == b'upgrade-media'
        assert Path(second['target']).with_suffix('.en.srt').read_bytes() == b'upgrade-subtitle'
        result = {'old_version': data['old_version'], 'new_version': __version__,
                  'legacy_undo': 'passed', 'saved_plan_apply': 'passed'}
        (root / 'upgrade-report.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
