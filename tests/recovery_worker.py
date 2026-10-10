"""Subprocess crash worker; never use fault hooks in normal service runs."""

import errno
import json
import os
import sys
from pathlib import Path

from jellyorganize.config import Config
from jellyorganize.filesystem import apply, durable, undo
from jellyorganize.planning.store import PlanStore


def main():
    payload = json.loads(Path(sys.argv[1]).read_text())
    config = Config.model_validate(payload["config"])
    if payload.get('download_files'):
        from jellyorganize.downloads import handoff
        class Client:
            def assert_no_active_overlap(self, torrent_id, paths, root):
                pass
            def torrent(self, torrent_id, *, include_files=False):
                return payload['torrent'], payload['download_files'] if include_files else []
        handoff.QBittorrentClient = lambda settings: Client()
    seen = 0

    def crash(stage):
        nonlocal seen
        if stage == payload["stage"]:
            seen += 1
            if seen == payload.get("occurrence", 1):
                os._exit(73)

    durable.checkpoint = crash
    apply.checkpoint = crash
    undo.checkpoint = crash
    if payload.get("publication_failure"):
        publish = durable.rename_noreplace
        def fail_publication(parent, source, destination):
            if destination.endswith(".mkv"):
                raise OSError("injected publication failure")
            return publish(parent, source, destination)
        durable.rename_noreplace = fail_publication
    if payload.get("copy"):
        def different_filesystem(*args, **kwargs):
            raise OSError(errno.EXDEV, "force copy branch")
        apply.os.link = different_filesystem
    if payload.get("rollback"):
        module = undo if payload["operation"] == "undo" else apply
        original = module._move_file

        def fail_media(file, source_root, destination_root, config):
            if file.source.suffix == (".srt" if payload["operation"] == "undo" else ".mkv"):
                raise OSError("injected media failure")
            return original(file, source_root, destination_root, config)

        module._move_file = fail_media
    if payload['operation'] == 'handoff':
        handoff.import_torrent(config, payload['torrent']['hash'])
    elif payload["operation"] == "undo":
        undo.undo_transaction(payload["transaction_id"], config)
    else:
        plan = PlanStore(config.state_dir / "plans").load(payload["plan_id"])
        apply.apply_plan(plan, config, config.state_dir / "transactions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
