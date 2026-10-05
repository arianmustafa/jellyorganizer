# 1.0 release validation

Version **1.0.0** targets Linux with Python 3.13 or newer. Validation uses synthetic files in isolated directories. It does not organize an existing library or modify real torrents. Optional systemd support is tested but is not required for manual organization or Qui hooks.

## Measured checks — 2026-10-05

| Check | Result |
| --- | --- |
| Local Python 3.13 suite | 270 tests passed. |
| Local Python 3.14.3 suite | 270 tests passed. |
| Media-host wheel suite | 270 tests passed against the 1.0 wheel, including physical cross-filesystem download handoff checks. |
| Frozen matching corpus | 40 cases; 15 automatic matches; zero wrong, unexpected, or missed expected automatic matches. |
| Live matching | 18 labeled cases; 15 automatic matches; three deliberately unresolved ambiguous Arrival releases; zero wrong, unexpected, or missed expected automatic matches. |
| Physical cross-filesystem apply/undo | 18 abrupt interruption checks and 25 repeated cycles passed on tmpfs/ext4, including source removal and journal commit gaps. |
| Download handoff recovery | Nine interruption checkpoints for handoff and undo, with both link and copy branches; nested files, repeated hooks, resumed torrents, rollback retry, and shared active torrent paths covered. |
| Installation and upgrade | Clean installed-wheel checks and upgrade from 0.4.12, preserving saved-plan application and completed legacy-transaction undo. |
| Optional user systemd | Generated units verified; one isolated empty-Incoming job recorded healthy state. No production timer enabled. |
| Short CLI soak | Completion acknowledgement, matching, ambiguity retention, exception deduplication, status, and undo passed. |

Raw machine reports and scratch paths are local validation artifacts, excluded from source control. Public configuration examples contain generic paths and addresses; private authentication data is never packaged.

Local validation covers both Python versions. GitHub Actions runs the complete validation workflow on pushes, pull requests, and manual dispatch; see the repository's Actions page for the result of each commit. Tagged releases require both matrix jobs to succeed. The longer optional soak was stopped during validation and is not claimed as completed. Physical power loss, storage controller failure, and other filesystems have not been validated.

The Arrival fixture was corrected after live verification found two distinct TMDb entries titled Arrival from 2016. These inputs remain unresolved without a provider ID; acceptance policy was not relaxed. Scores are evidence rules, not calibrated probabilities. This small curated matching corpus does not estimate a population error rate.

## Downloader activation

The qBittorrent adapter and Qui hook are tested with synthetic API responses, including login, proxy authentication, API prefixes, active torrent rejection, file selection, and shared paths. A live API rejecting unauthenticated requests still needs the operator's credentials before activation. The release does not change authentication settings, seeding limits, installed applications, or Qui rules on the validation host.

Configure the generic sections in [config.example.toml](config.example.toml), save credentials with `credentials`, and run `config check --download-client`. Register the executable and the stopped/completed rule in Qui as documented in [README.md](README.md). Test a selected torrent with `import-download --torrent HASH --dry-run` before enabling its automatic rule.

## GitHub release workflow

The repository remains private until its owner changes its visibility. Release automation does not change repository visibility or publish to PyPI.

1. Update the version in `pyproject.toml` and `jellyorganize/__init__.py`, and add the matching `## VERSION` section in `CHANGELOG.md`. Update versioned install examples as needed.
2. Run `python scripts/release.py check --tag vVERSION`, the tests, and the frozen benchmark. Commit the changes and push them to the repository.
3. Tag that commit and push the tag, for example:

   ```bash
   git tag -a v1.0.0 -m "Jellyorganize 1.0.0"
   git push origin v1.0.0
   ```

4. GitHub Actions validates Python 3.13 and 3.14, builds the packages, checks a clean wheel installation, and creates a **draft** GitHub release with the Python 3.13 wheel, source archive, and `SHA256SUMS`.
5. Inspect the draft's notes and downloads, then publish it in GitHub when ready. Release publication and repository visibility are separate settings; a published release in a private repository still requires repository access.

The tag must exactly match the package version with a `v` prefix. Versions with an `a`, `b`, or `rc` suffix are also marked as prereleases in GitHub. A failed validation prevents release creation. Rerunning a successful tag workflow refreshes an existing draft; it refuses to modify an already published release. Only the release job has `contents: write`; validation uses read access and synthetic metadata without media credentials. No additional repository secrets are required.

To prepare release files locally after building into a clean distribution directory, run `python scripts/release.py prepare --dist PATH`. It rejects stale versions and writes checksums plus release notes from the matching changelog section. Check a downloaded archive with `sha256sum --check SHA256SUMS` from its download directory.

## Reproduce

```bash
python -m pytest -q
jellyorganize benchmark --output matching-report.json
jellyorganize benchmark --live --output live-matching-report.json
python scripts/validate_storage.py --library-parent /path/on/another/filesystem --cycles 25
JELLYORGANIZE_TEST_HANDOFF_PARENT=/path/on/another/filesystem python -m pytest -q tests/test_downloads.py
python scripts/validate_service.py --execute
python scripts/soak_incoming.py --cycles 3 --interval 0
python scripts/soak_incoming.py --hours 24 --interval 60
```

The soak creates fresh media, configuration, cache, and state roots. It uses frozen provider fixtures without credentials, writes `status.json` after each cycle, and stops at its configured duration/cycle count. A quick check is not equivalent to a full unattended soak.

For upgrades, copy `scripts/validate_upgrade.py` outside the checkout, run its `prepare NEW_SCRATCH_PATH` phase with the old environment, install the new wheel into an isolated environment, then run `verify SCRATCH_PATH`. Keep fixtures separate from production state. Upgrade the real environment only after retaining its configuration, credential files, and transaction state.
