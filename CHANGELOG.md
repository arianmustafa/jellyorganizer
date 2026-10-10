# Changelog

## 1.2.0 — 2026-10-10

- Import completed qBittorrent torrents while they continue seeding in hard-link
  mode; move mode still requires stopped torrents.
- Save parsed titles, effective matching hints, and candidate comparisons in
  plans. Show evidence during review and with the offline `explain` command.
- Add opt-in movie versions with resolution, edition, or explicit labels using
  Jellyfin's naming layout. Preserve duplicate checks, subtitles, and undo.
- Add `doctor` to probe directory access and actual hard-link support, cleaning
  up disposable probe files afterward.
- Add optional JSON webhooks for new or changed exceptions and failures, with
  private credentials, durable retries, and deduplicated alerts.
- Notify Jellyfin of successful library imports and repairs using targeted path
  updates, optional server mount mapping, and crash-safe retries. Server failures
  leave imports intact; retry with `integrations` or the next automatic run.

## 1.1.0 — 2026-10-10

- Add optional `filesystem.mode = "hardlink"` for Incoming and qBittorrent imports.
  Original media and companion files remain in place and share data with library
  entries. Unsupported or cross-filesystem links fail without copying.
- Record transfer mode in saved plans and journals; make link publication,
  rollback, recovery, and undo restartable while retaining legacy move behavior.
- Recognize completed links on repeated runs, repair missing links, and follow
  recorded library audits without recreating obsolete names.
- Restore a hyphenated movie title when GuessIt misreads its leading word as a
  release group and the containing folder corroborates the full title and year.
  Keep ambiguous searches and conflicting provider details unresolved.

## 1.0.0 — 2026-10-05

* Organize completed Incoming movies and TV automatically; unresolved items remain while unrelated strong matches proceed.
* Resume interrupted apply, undo, and rollback using durable write-ahead receipts. Stage verified copies before publication, synchronize media and directories, and never overwrite another file.
* Track download completion across runs, accept downloader completion acknowledgements, or support atomic completed-download handoffs.
* Wait once for file stability during manual organization, then rescan; scheduled runs remain nonblocking.
* Add configurable qBittorrent/Qui hooks that move completed, stopped torrents into Incoming, preserve nested folders, organize media automatically, and retain torrent entries. Check shared torrent paths, deduplicate repeated hooks, and recover interrupted handoff and undo.
* Add strict versioned TOML configuration with `config init`, `show`, and `check`, retaining legacy Incoming paths and warning about retired settings.
* Add scheduled `run`, persistent health and deduplicated exceptions, bounded retries, and generated user systemd units.
* Save credentials through hidden prompts or environment variables. Support WebUI and reverse-proxy authentication and a read-only connection check.
* Add frozen/live labeled matching benchmarks, crash recovery tests, installation and upgrade validators, and a bounded unattended soak harness.
* Fix cross-filesystem undo rollback recovery when copying changes an inode; keep provider failures retryable even when a run also contains conflicts.

* Publish portable examples, MIT licensing, packaged configuration and matching fixtures, and Python 3.13/3.14 release checks.
* Build tested GitHub release drafts from matching version tags, including the wheel, source archive, changelog notes, and SHA-256 checksums. Keep repository visibility and final release publication under the owner's control.

See RELEASE.md for measured validation and its limits. Live downloader activation requires the operator's API credentials and Qui rule configuration.
