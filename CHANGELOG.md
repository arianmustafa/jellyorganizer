# Changelog

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
