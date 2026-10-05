"""Release gating must reject mismatched tags and stale distribution files."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('release_script', Path(__file__).resolve().parents[1] / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def project(tmp_path):
    (tmp_path / 'jellyorganize').mkdir()
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "1.0.0"\n')
    (tmp_path / 'jellyorganize/__init__.py').write_text('__version__ = "1.0.0"\n')
    (tmp_path / 'CHANGELOG.md').write_text('# Changelog\n\n## 1.0.0 — date\n\nCurrent release.\n\n## 0.4.12\nOld release.\n')
    return tmp_path


def test_matching_tag_and_only_current_release_notes(tmp_path):
    root = project(tmp_path)
    assert release.version(root, 'v1.0.0') == '1.0.0'
    assert release.notes(root, '1.0.0') == 'Current release.\n'
    with pytest.raises(ValueError, match='release tag must'):
        release.version(root, 'v1.0.1')
    (root / 'jellyorganize/__init__.py').write_text('__version__ = "1.0.1"\n')
    with pytest.raises(ValueError, match='versions must match'):
        release.version(root, 'v1.0.0')


def test_artifacts_checksums_and_rerun(tmp_path):
    root = project(tmp_path)
    distribution = root / 'dist'
    distribution.mkdir()
    files = ['jellyorganize-1.0.0-py3-none-any.whl', 'jellyorganize-1.0.0.tar.gz']
    for name in files:
        (distribution / name).write_bytes(name.encode())
    release.prepare(root, distribution, '1.0.0')
    first = (distribution / 'SHA256SUMS').read_text()
    assert first == ''.join(f'{hashlib.sha256(name.encode()).hexdigest()}  {name}\n' for name in files)
    assert (distribution / 'RELEASE_NOTES.md').read_text() == 'Current release.\n'
    release.prepare(root, distribution, '1.0.0')
    assert (distribution / 'SHA256SUMS').read_text() == first
    (distribution / 'jellyorganize-0.4.12-py3-none-any.whl').write_bytes(b'stale')
    with pytest.raises(ValueError, match='exactly this version'):
        release.prepare(root, distribution, '1.0.0')


def test_missing_changelog_or_distribution_blocks_release(tmp_path):
    root = project(tmp_path)
    with pytest.raises(ValueError, match='exactly this version'):
        release.prepare(root, root / 'missing', '1.0.0')
    with pytest.raises(ValueError, match='changelog must contain'):
        release.notes(root, '1.1.0')
