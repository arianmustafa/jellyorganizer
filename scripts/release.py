"""Validate a release version and prepare offline GitHub release artifacts."""

import argparse
import ast
import hashlib
import re
import tomllib
from pathlib import Path


def version(root, tag=''):
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    module = ast.parse((root / 'jellyorganize/__init__.py').read_text())
    declared = [ast.literal_eval(node.value) for node in module.body
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__'
                                                        for target in node.targets)]
    if declared != [project]:
        raise ValueError('package and module versions must match')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?', project):
        raise ValueError('release version must use MAJOR.MINOR.PATCH with an optional a/b/rc suffix')
    if tag and tag != 'v' + project:
        raise ValueError(f'release tag must be v{project}, got {tag!r}')
    return project


def notes(root, release_version):
    text = (root / 'CHANGELOG.md').read_text()
    heading = re.search(r'^## ' + re.escape(release_version) + r'(?:\s.*)?$', text, re.M)
    if heading is None:
        raise ValueError('changelog must contain a section for the release version')
    body = text[heading.end():]
    next_heading = re.search(r'^## ', body, re.M)
    if next_heading:
        body = body[:next_heading.start()]
    if not body.strip():
        raise ValueError('release changelog section is empty')
    return body.strip() + '\n'


def prepare(root, distribution, release_version):
    expected = [distribution / f'jellyorganize-{release_version}-py3-none-any.whl',
                distribution / f'jellyorganize-{release_version}.tar.gz']
    actual = sorted([*distribution.glob('*.whl'), *distribution.glob('*.tar.gz')])
    if actual != sorted(expected) or not all(path.is_file() for path in expected):
        raise ValueError('distribution directory must contain exactly this version\'s wheel and source archive')
    checksums = []
    for path in expected:
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        checksums.append(f'{digest}  {path.name}\n')
    (distribution / 'SHA256SUMS').write_text(''.join(checksums))
    (distribution / 'RELEASE_NOTES.md').write_text(notes(root, release_version))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'prepare'])
    parser.add_argument('--tag', default='')
    parser.add_argument('--dist', type=Path, default=Path('dist'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        release_version = version(root, args.tag)
        notes(root, release_version)
        if args.action == 'prepare':
            prepare(root, args.dist, release_version)
        print(f'Release {release_version}: {args.action} passed')
        return 0
    except (OSError, ValueError, KeyError, SyntaxError) as error:
        parser.exit(1, f'Release error: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
