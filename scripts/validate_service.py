"""Verify generated units and optionally run an isolated empty Incoming job.

Does not install persistent units or enable timers. Requires user systemd.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from jellyorganize.config import load_config
from jellyorganize.service import install


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='run one transient empty Incoming service')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='jellyorganize-service-test-'))
    for directory in ('Incoming', 'Movies', 'TV'):
        (root / directory).mkdir()
    path = root / 'config.toml'
    path.write_text(f'''schema_version = 1
[incoming]
path = "{root / 'Incoming'}"
[movies]
library = "{root / 'Movies'}"
[tv]
library = "{root / 'TV'}"
[service]
credential_file = "{root / 'unused-private-token'}"
''')
    directory = install(load_config(path), path, root / 'units')
    verified = subprocess.run(['systemd-analyze', '--user', 'verify',
                               str(directory / 'jellyorganize.service'), str(directory / 'jellyorganize.timer')],
                              capture_output=True, text=True, timeout=30)
    assert verified.returncode == 0, verified.stderr
    result = {'unit_verification': 'passed', 'scratch': str(root), 'transient_run': 'not requested'}
    if args.execute:
        environment = {'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                       'XDG_STATE_HOME': str(root / 'state'), 'XDG_DATA_HOME': str(root / 'data'),
                       'XDG_CACHE_HOME': str(root / 'cache')}
        command = ['systemd-run', '--user', '--wait', '--collect', '--service-type=oneshot',
                   '--unit=' + root.name, '--property=NoNewPrivileges=yes', '--property=UMask=0077']
        command.extend('--setenv=' + key + '=' + value for key, value in environment.items())
        command.extend([sys.executable, '-m', 'jellyorganize.cli', '--config', str(path), 'run'])
        run = subprocess.run(command, capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr + run.stdout
        env = {**os.environ, **environment}
        status = subprocess.run([sys.executable, '-m', 'jellyorganize.cli', '--config', str(path), 'status', '--json'],
                                env=env, capture_output=True, text=True, check=True, timeout=30)
        health = json.loads(status.stdout)
        assert health['last_run']['exit_code'] == 0 and health['open_exceptions'] == 0
        result['transient_run'] = 'passed'
        result['timer_enabled'] = False
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
