<p align="center">
  <img src="docs/assets/jellyorganize-logo.png" width="200" alt="Jellyorganize logo: a violet jellyfish with a play button and teal media spines">
</p>

# Jellyorganize

[![CI](https://github.com/arianmustafa/jellyorganizer/actions/workflows/release.yml/badge.svg)](https://github.com/arianmustafa/jellyorganizer/actions/workflows/release.yml)
[![Version 1.2.0](https://img.shields.io/badge/version-1.2.0-7c3aed)](https://github.com/arianmustafa/jellyorganizer/releases)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Linux](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black)](#install-and-configure)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

Automatically organize completed movies and TV episodes from Incoming into Jellyfin libraries. Run `jellyorganize organize`, or schedule `jellyorganize run`; strong matches move immediately by default. Optional hard-link mode retains originals without duplicating file data. Uncertain files stay in Incoming while other files proceed. No routine audit, review, or apply step is required. Existing Movies and TV libraries are repaired only through an explicit `audit` command.

Version 1.0 adds durable recovery, completion tracking, persistent exceptions, strict TOML configuration, optional qBittorrent/Qui automation, and a reproducible matching benchmark. Linux and Python 3.13+ are required. No root access is needed.

## Install and configure

Install the wheel on the media host in a virtual environment. For a new installation:

```bash
uv venv --python 3.13 ~/.venvs/jellyorganize
uv pip install --python ~/.venvs/jellyorganize/bin/python ./jellyorganize-1.2.0-py3-none-any.whl
source ~/.venvs/jellyorganize/bin/activate
jellyorganize config init
```

For an existing installation, retain its configuration and state directories and upgrade the same environment:

```bash
uv pip install --upgrade --python ~/.venvs/jellyorganize/bin/python ./jellyorganize-1.2.0-py3-none-any.whl
```

Alternatively, use `python3 -m venv` and the environment's `pip`.

Release files are available from this repository's [GitHub Releases](https://github.com/arianmustafa/jellyorganizer/releases). Access follows repository visibility. This project is not yet published to PyPI.

For an isolated CLI installation, install a downloaded wheel with either:

```bash
uv tool install --python 3.13 ./jellyorganize-1.2.0-py3-none-any.whl
# Or, with Python 3.13+ available:
pipx install --python python3.13 ./jellyorganize-1.2.0-py3-none-any.whl
```

From a source checkout, activate a Python 3.13+ virtual environment and install dependencies with `python -m pip install -e .`. You can then run `python -m jellyorganize.cli organize` directly. Both forms use the same configuration and workflow. No Docker image or standalone binary is provided.

Configuration lives at **`~/.config/jellyorganize/config.toml`**, respecting `XDG_CONFIG_HOME`. `config init` creates a commented template with private permissions and never overwrites an existing file. Edit that file to set your paths and completion policy, then run:

```bash
jellyorganize config show
jellyorganize config check
jellyorganize doctor
jellyorganize organize --dry-run
jellyorganize organize
```

Use a different file with the global option: `jellyorganize --config /absolute/path/settings.toml run`. Explicitly naming a missing file is an error. Unknown settings, unsupported providers, relative paths, overlapping media roots, and disabled cross-filesystem verification are rejected. `config show` displays effective settings without credentials; `config check` verifies directory access and reports credential availability.

Before organizing uncached media, save your TMDb token with `jellyorganize credentials tmdb`, or supply it through the `TMDB_API_TOKEN` environment variable. See the credentials section below.

The complete template is [config.example.toml](config.example.toml). A typical configuration is:

```toml
schema_version = 1

[incoming]
path = "~/media/Incoming"
completion = "stable"
stability_seconds = 60
minimum_age_seconds = 60

[movies]
library = "~/media/Movies"

[tv]
library = "~/media/TV Shows"

[service]
interval_seconds = 300
credential_file = "~/.local/share/jellyorganize/tmdb.token"
```

Directories must already exist. Commands that load configuration do not create media directories. Previous configurations containing `[movies].incoming` or `[tv].incoming` continue to use separate Incoming folders unless `[incoming].path` is explicitly set. The obsolete `matching.review_threshold` setting is accepted with a migration warning; remove it. Configurations use schema version 1. New saved plans use version 2 and new journals use version 3 to record transfer mode. Older plans and journals retain move semantics, including recovery and undo.

## Download completion

Choose the policy that fits your downloader:

| `incoming.completion` | When a file becomes eligible |
| --- | --- |
| `stable` (default) | Media and associated sidecars remain unchanged for `stability_seconds`. `organize` waits once and rechecks; `run` observes across separate passes without waiting. |
| `marker` | A downloader hook calls `jellyorganize ready /absolute/path/to/file-or-folder` after completion. Any subsequent file or sidecar change invalidates that acknowledgement. |
| `handoff` | Your downloader places only completed files into Incoming through an atomic handoff from an external staging directory. |

The minimum age and temporary-file checks apply to all modes. Temporary extensions include `.part`, `.partial`, `.tmp`, and `.crdownload`. Stability is a heuristic: a paused downloader can look stable. Use `marker` or atomic `handoff` when your downloader can give an explicit completion signal. A first scheduled `run` usually records observations; `organize` can observe and organize in one invocation. Files changed during the wait remain untouched until a later invocation.

In a shared Incoming folder, season/episode codes route files to TV, while a movie year without episode codes routes to movies. Ambiguous media types, ambiguous sidecars, extras, duplicates, and incomplete packages remain untouched. Associated subtitles and other supported sidecars move with their media.

## Hard-link imports

To keep original files in Incoming and qBittorrent downloads, set:

```toml
[filesystem]
mode = "hardlink"
```

The default is `mode = "move"`. Hard-link mode creates library entries sharing
the original files' data, including associated subtitles. Downloads → Incoming
and Incoming → Movies/TV both retain their source paths. All participating paths
must be on compatible mounts of the same filesystem. If hard links are unavailable,
the import fails while preserving originals; it never falls back to copying.
`config show`, `config check`, and `organize --dry-run` display the chosen mode.

Hard links are independent filenames for the same file data: editing through any
name changes every linked name. Removing a name preserves the data while another
link remains. Files still need to satisfy the selected completion policy.
qBittorrent imports accept fully downloaded torrents that are stopped or seeding
in hard-link mode, preserving download paths so seeding can continue during import.
Checking, moving, errored, or incomplete downloads remain untouched. Move mode
requires stopped torrents.

Repeated runs recognize unchanged completed links and skip them without reporting
duplicate conflicts. Missing links can be repaired from the retained original and
saved identity. Changed originals, replacement destinations, and newly added
companions undergo checks again; existing data is never overwritten.

Recovery and undo use the mode saved with the operation, even if the configuration
later changes. Hard-link undo removes only links created by that transaction after
verifying the surviving original and file state. A repair transaction leaves links
that were already present alone. If a source is missing or altered, undo preserves
the destination for inspection. A later scheduled run can recreate undone links
while their originals remain in Incoming; remove those files from Incoming to keep
them out of subsequent imports.

`audit` continues to relocate library filenames. Tracked hard links must remain on
compatible mounts during audit, and scheduled imports follow recorded audit
relocations instead of recreating obsolete names. Undo an audit before undoing the
original import. For downloader imports, undo the library import first and the
downloads → Incoming handoff second.

## Filesystem checks

Run `jellyorganize doctor` before importing, or use `doctor --json` for scripts.
It checks each configured import stage with small disposable files, verifies
actual hard-link inode identity, and cleans up its probes. Missing directories
are reported without creating them. In hard-link mode, incompatible mounts or
link permissions fail the check; move mode also checks destination writes when
links are unavailable. Exit code `4` means a check failed.

## Multiple movie versions

Enable separate versions of the same movie with:

```toml
[naming]
movie_versions = true
```

The label comes from the edition and resolution parsed from the filename, such
as `1080p`, `2160p`, or `Extended 2160p`. For a specific label, add
`[version-Theatrical]` or `[version-Director's Cut]` to the incoming filename.
Files without these hints use `Original`. Subtitles receive the same version
prefix. The resulting layout follows [Jellyfin's multiple-version naming rules](https://jellyfin.org/docs/general/server/media/movies/#multiple-versions):

```text
Movies/Dune (2021) [tmdbid-438631]/
  Dune (2021) [tmdbid-438631] - 1080p.mkv
  Dune (2021) [tmdbid-438631] - 1080p.en.srt
  Dune (2021) [tmdbid-438631] - Extended 2160p.mkv
```

Different labels coexist; the same label, including a different extension or
letter case, remains a conflict. No version replaces another automatically.
An existing movie without a version label blocks additions until you explicitly
audit it into the version layout. Saved plans keep their destination filenames
even if this setting changes before apply. Movie versions are disabled by default.

## Unattended operation and credentials

TMDb requires `TMDB_API_TOKEN` for uncached requests; TVmaze needs no key. An environment token takes precedence over the configured private credential file. Credentials belong outside TOML. The credential file must be a regular file owned by your user, with permissions `600`.

Save your token with a hidden prompt, then check the configuration and organize Incoming:

```bash
jellyorganize credentials tmdb
jellyorganize config check
jellyorganize organize
```

To save a token already supplied through `TMDB_API_TOKEN`, run `jellyorganize credentials tmdb --from-env`. Both credential commands save the configured private file without printing its value or replacing a different existing credential.

`organize` handles matching and moves in one invocation, leaving uncertain files alone. A background service is optional. For a scheduler such as cron, use `run` for one pass with compact reporting. Configure hooks and scheduled jobs with the installed executable's absolute path and make the configured credential file available to the user running the command.

For a downloader completion hook, set `completion = "marker"` and call `jellyorganize ready /absolute/path/to/completed/download`, then `jellyorganize run`. To see retained results:

```bash
jellyorganize status
jellyorganize exceptions
```

`run` prints one JSON summary per run, including its transaction ID for undo. Exceptions persist and are deduplicated by source, status, and reason; an unchanged problem increments its occurrence count without becoming a new alert. Files are retried on subsequent runs and exceptions resolve after the source is processed successfully. Run history retains the latest 1,000 summaries. Plans and transaction journals are retained for recovery and undo; monitor state-directory space and keep them as long as undo is needed.

On hosts where you choose systemd, `service show` displays the optional generated units and `service install --save-credential` writes units plus the private token for scheduled jobs. Installation does not enable the timer. The timer runs Incoming only, every `service.interval_seconds`; failures have bounded restart retries. User timers require an active user systemd manager. Systemd is not required to organize media or read the configuration.

## Notifications and Jellyfin refresh

Both integrations are optional and disabled by default. Enable either in TOML:

```toml
[notifications]
enabled = true
webhook_url_file = "~/.local/share/jellyorganize/webhook.url"

[jellyfin]
enabled = true
url = "http://127.0.0.1:8096"
api_key_file = "~/.local/share/jellyorganize/jellyfin.key"
# Optional paths as seen by the Jellyfin server, such as container mounts:
# movies_path = "/media/movies"
# tv_path = "/media/tv"
```

Save credentials with hidden prompts:

```bash
jellyorganize credentials webhook
jellyorganize credentials jellyfin
jellyorganize config check
```

Enter the complete webhook URL and a Jellyfin API key respectively. Environment
alternatives are `JELLYORGANIZE_WEBHOOK_URL` and `JELLYFIN_API_KEY`; use
`credentials PROVIDER --from-env` to save an existing value. Webhook URLs remain
outside TOML because their path or query can contain secrets. Credentials are
excluded from plans, journals, and integration error messages.

The webhook receives generic JSON from automatic runs and explicit apply:

```json
{
  "event": "exceptions",
  "event_id": "webhook:PLAN_ID:RUN_ID",
  "plan_id": "PLAN_ID",
  "exceptions": [{"source": "/media/Incoming/example.mkv", "kind": "movie", "status": "REVIEW", "reason": "ambiguous or unmatched TMDb search"}]
}
```

Use an endpoint that accepts this JSON, or an adapter for your notification
service. Only newly seen or changed problems generate alerts. Fatal failures
use `event = "failure"` with an `error` field; identical consecutive failures
are deduplicated. Delivery is at least once: the same `event_id` and
`Idempotency-Key` header are retained across retries so receivers can deduplicate.

After successful library imports and audits, Jellyfin receives targeted
`/Library/Media/Updated` requests for changed media and companions. Audits also
report the old paths as deleted. Downloads → Incoming handoffs, unchanged reused
links, dry runs, and failed or rolled-back items do not trigger refreshes. The
server URL and optional mount mapping are saved with each operation. A refresh
queued for one server will not be sent to a different configured server.

Problems with either server leave media results intact. Pending deliveries
survive restarts and retry on the next automatic run, apply, recovery, or
`jellyorganize integrations`. `integrations --json` gives delivery counts and
errors; `status` reports the number pending. HTTP redirects are refused, requests
have a five-second timeout, and each pass attempts at most twenty deliveries,
pausing a failed channel until the next pass. Disabling an integration pauses its
pending deliveries. Enabling refresh does not replay older imports performed
while it was disabled. Undo changes local files; use Jellyfin's normal scan to
update its view after an undo.

## qBittorrent and Qui automation

The optional hook checks qBittorrent's current state, imports a fully downloaded torrent into Incoming, then automatically organizes eligible media. Move mode requires the torrent to be **stopped**. Hard-link mode accepts completed seeders and preserves their original paths while importing. The hook retains torrent entries and never sends deletion, stop, or resume requests. Configure your seeding policy separately.

Add these sections to your configuration, replacing the examples with your own paths and API address:

```toml
[downloads]
path = "~/downloads/qbittorrent"

[qbittorrent]
url = "http://127.0.0.1:8080"
username = "your-webui-user"
password_file = "~/.local/share/jellyorganize/qbittorrent.password"
```

Use a shared `[incoming].path` and separate downloads, Incoming, Movies, and TV directories. A proxied API URL can include a prefix, such as `https://media.example.org/qbittorrent`. Leave `username` empty only if the API allows unauthenticated access. For separate reverse-proxy HTTP Basic authentication, configure `basic_username` and `basic_password_file` as shown in the complete template. URL credentials are rejected; passwords stay outside TOML.

```bash
jellyorganize credentials qbittorrent
# Only if the reverse proxy requires HTTP Basic authentication:
jellyorganize credentials qbittorrent-proxy
jellyorganize config check --download-client
```

The prompts hide passwords. Environment alternatives are `QBITTORRENT_PASSWORD` and `QBITTORRENT_BASIC_PASSWORD`; `credentials PROVIDER --from-env` can store them privately. Checking the connection authenticates and reads the application version without changing torrents.

In [Qui External Programs](https://github.com/autobrr/qui/blob/develop/documentation/docs/features/external-programs.md), register and allowlist the installed executable's absolute path, for example `/home/your-user/.venvs/jellyorganize/bin/jellyorganize`. Set its arguments to:

```text
import-download --torrent "{hash}"
```

Create a [Qui automation rule](https://github.com/autobrr/qui/blob/develop/documentation/docs/features/automations.md) with **Progress = 100%** and an appropriate media category or tag. Move mode also needs **State = stopped**; hard-link mode can run while the completed torrent is seeding. Its action runs the registered external program. Run Qui's program test against an eligible torrent with `--dry-run` added first. Use the installed executable's absolute path. Qui executes programs on its backend host; if it runs in a container, the executable, configuration, credentials, media paths, and state must all be available inside that container.

The hook verifies the exact torrent hash, selected-file completion and sizes, current state, other torrents sharing the same paths, path containment, and destination collisions independently of Qui's rule. qBittorrent 4's `pausedUP` and 5's `stoppedUP` are supported. Hard-link mode also accepts `uploading`, `stalledUP`, `queuedUP`, and `forcedUP` with progress exactly 100% and no bytes remaining; incomplete overlapping torrents still block import. A previously verified interrupted handoff can recover from `missingFiles`; a new import in that state is rejected. Repeated rule evaluations reuse the durable hash record rather than moving the same torrent twice. An HTTP or filesystem failure leaves work retryable; a metadata failure leaves the completed download in Incoming, where `organize` or `run` can retry it.

Nested folders are preserved during handoff:

```text
downloads/Show/Season 2/Show.S02E03.mkv
                  ↓
Incoming/Show/Season 2/Show.S02E03.mkv
                  ↓
TV Shows/Show (year) [tmdbid-ID]/Season 02/…mkv
```

Selected subtitles and release files retain their relative paths into Incoming. Supported sidecars then follow their media into the library; unknown notes or ambiguous media stay in Incoming. Unselected files remain in downloads, and empty directories are retained. In move mode, the kept torrent entry points at its old download paths and may display missing files after a recheck; resuming it requires undoing the moves first. Hard-link mode preserves those download paths. To undo the whole workflow, undo the organization transaction, then the handoff transaction. The hook prints both transaction IDs as JSON.

For manual testing, use `import-download --torrent HASH --dry-run`; `--handoff-only` imports into Incoming without organizing. The qBittorrent API and filesystem cannot be locked together: avoid rechecking, relocating, or changing download contents during handoff. In move mode, keep the torrent stopped throughout import.

## Automatic matching

The automatic floor is **0.97**, or the configured threshold if higher. Scores represent evidence rules, not calibrated probabilities.

Review shows the parsed title/year, effective matching title/year, and whether
each saved candidate agrees. Inspect this evidence later without network access
using `jellyorganize explain PLAN_ID`, or `explain PLAN_ID --json`. Older plans
remain usable; create a new ingest or audit plan to save matching evidence.

* Movies can score 0.98 when exactly one TMDb result matches the local title and year and a details lookup confirms its identity. Every search page is read, up to ten; incomplete searches cannot establish uniqueness. Missing years, fuzzy matches, contradictions, and multiple exact candidates remain unresolved. This uses one provider. Set `[matching] confirm_exact_movies = false` to require an explicit or manually saved identity instead.
* TV uses TMDb and TVmaze title/year and episode evidence, with shared external IDs supporting yearless series. A single titled episode with different release numbering can map to TMDb's episode when both providers uniquely agree on its title, season, and series IDs. Missing or contradictory evidence leaves it untouched.
* Explicit verified TMDb IDs and saved identities score 1.0. Existing-library movie audits retain their manual identity requirement.

Run `jellyorganize benchmark` for the packaged 40-case frozen regression corpus. It contains release styles and deliberately contradictory metadata examples. `benchmark --live` checks labeled positive examples and known title/year ambiguity against live providers. `--corpus FILE` accepts additional labeled cases; `--output FILE` saves the detailed JSON report. A pass on this small curated corpus is regression evidence, not a measured error rate for your entire library. Labels for automatic cases contain `expected.tmdb_id` and, for TV, `expected.season` and `expected.episodes`; the packaged JSON documents the format.

## Recovery and undo

Every apply saves an immutable plan and writes durable file receipts before transferring files. Links and copies are staged under hidden `.jellyorganize-*.part` names and published without overwriting; cross-filesystem copies receive full content-hash verification. In move mode, source removal occurs after destination publication and directory synchronization. Hard-link mode retains sources. Apply, undo, and recovery share a media lock.

An automatic run first resumes interrupted transactions using their saved decisions, without guessing identities again. Run `jellyorganize recover` explicitly to see recovery results. Changed files, occupied paths, altered roots, or incomplete legacy journals block unsafe recovery and preserve data. Correct the reported condition before retrying. Unknown staging files are preserved when ownership cannot be established; inspect these rather than deleting them blindly. The guarantee depends on the filesystem honoring synchronization; power-loss behavior on other storage types has not been validated.

```bash
jellyorganize undo TRANSACTION_ID
```

Undo verifies recorded file state, restores moved media and sidecars or removes created hard links, and writes its own recoverable journal. It refuses changed files, occupied restoration paths, and missing retained originals. Repeated completed undo is harmless. Keep the original plan and all related journals. Empty directories are retained. Configured roots and paths beneath them cannot be symlinks; a symlink in an ancestor of a root is supported.

State is stored under `~/.local/state/jellyorganize/`, identities under `~/.local/share/jellyorganize/`, and metadata cache under `~/.cache/jellyorganize/`; XDG overrides are supported. Do not share a state directory between independent hosts or configurations.

## Commands and exit codes

| Command | Purpose |
| --- | --- |
| `organize [all\|movies\|tv] [--dry-run]` | Automatically organize Incoming, with detailed output. |
| `run [all\|movies\|tv]` | Same Incoming workflow with compact persistent run reporting. |
| `scan incoming\|library all\|movies\|tv` | Show files found by scanning; both arguments are required. |
| `config init\|show\|check` | Create, inspect, or validate configuration. |
| `config check --download-client` | Also authenticate and test the configured qBittorrent API. |
| `credentials tmdb\|qbittorrent\|qbittorrent-proxy` | Save a private credential with a hidden prompt or `--from-env`. |
| `import-download --torrent HASH` | Import a completed torrent and organize Incoming; hard-link mode allows seeding. |
| `ready PATH` | Acknowledge a completed download for marker mode. |
| `status`, `exceptions` | Show health and unresolved files; both support `--json`. |
| `service install\|show` | Generate or display user systemd units. |
| `recover`, `undo TRANSACTION_ID` | Resume interrupted work or reverse completed moves. |
| `benchmark` | Run labeled matching checks. |
| `doctor [--json]` | Probe directory access and actual hard-link support. |
| `explain PLAN_ID [--json]` | Inspect saved matching evidence without provider requests. |
| `integrations [--json]` | Retry pending webhooks and targeted Jellyfin refreshes. |
| `cache stats\|clear` | Inspect or clear cached metadata. |
| `ingest [all\|movies\|tv]` | Save an Incoming plan; `--auto` also applies eligible entries. |
| `audit [all\|movies\|tv]` | Explicitly plan existing-library repairs; `--auto` applies strong repairs. |
| `review PLAN_ID`, `apply PLAN_ID`, `identify PATH` | Optional tools for resolving individual exceptions; see `--help`. |

Exit codes: `0` completed; `1` operation/provider/recovery failure; `2` unresolved review items (also argument-parser usage errors); `3` conflicts; `4` configuration/setup error; `130` interrupted. The systemd unit treats 2 and 3 as completed runs because unrelated eligible items still proceed. Operation/provider failures take precedence over conflicts and reviews so scheduled retries still occur.

## Development and release validation

```bash
python -m pip install -e '.[test]' build
python -m pytest -q
jellyorganize benchmark
python scripts/validate_storage.py --cycles 25 --output storage-report.json
python -m build
```

The storage validator creates fresh scratch directories with synthetic media and known identities. Set `--library-parent /path/on/another/filesystem` to test real cross-filesystem moves. It abruptly kills apply/undo subprocesses at nine checkpoints each and verifies recovery, content, and repeated runs. It never scans existing media. For physical cross-filesystem handoff tests, run `JELLYORGANIZE_TEST_HANDOFF_PARENT=/path/on/another/filesystem python -m pytest -q tests/test_downloads.py`; each test creates its own fresh directory there. These tests use synthetic API responses and make no real torrent changes. Scratch directories and reports are retained for inspection. CI is configured to check Python 3.13/3.14, tests, matching, storage recovery, and a clean wheel installation. See [RELEASE.md](RELEASE.md) for measured results.

## License

[MIT](LICENSE).
