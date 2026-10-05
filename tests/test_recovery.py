import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.filesystem.recovery import recover
from jellyorganize.planning.store import PlanStore
from test_apply import confirmed_plan, touch


STAGES = ["intent_written", "stage_created", "stage_recorded", "stage_verified", "published",
          "source_removed", "move_recorded", "item_committed", "transaction_committed"]


def preparation(config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source, b"movie-data" * 256000)
    sidecar = source.with_suffix(".en.srt")
    touch(sidecar, b"subtitle-data")
    config.movies.library.mkdir(parents=True)
    plan = confirmed_plan(config, config.state_dir, source)
    return source, sidecar, plan


def worker(config, tmp_path, payload):
    file = tmp_path / "worker.json"
    file.write_text(json.dumps({"config": config.model_dump(mode="json"), **payload}))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("recovery_worker.py")), str(file)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, result.stdout + result.stderr


@pytest.mark.parametrize("operation", ["apply", "undo"])
@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.parametrize("stage", STAGES)
def test_killed_apply_and_undo_resume_without_loss(config, tmp_path, monkeypatch, operation, copy, stage):
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    payload = {"plan_id": plan.plan_id, "operation": operation, "stage": stage, "copy": copy}
    if operation == "undo":
        transaction, _ = apply_plan(plan, config, config.state_dir / "transactions")
        payload["transaction_id"] = transaction.data["transaction_id"]
    worker(config, tmp_path, payload)
    counts, errors = recover(config)
    assert errors == [], errors
    target = plan.entries[0].destination
    expected = source if operation == "undo" else target
    absent = target if operation == "undo" else source
    assert expected.read_bytes() == b"movie-data" * 256000
    assert expected.with_suffix(".en.srt").read_bytes() == b"subtitle-data"
    assert not absent.exists() and not absent.with_suffix(".en.srt").exists()
    counts, errors = recover(config)
    assert counts == {"RECOVERED": 0, "BLOCKED": 0} and not errors


def test_killed_partial_copy_is_restarted_before_publication(config, tmp_path, monkeypatch):
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    # The sidecar is move one; kill during the first video chunk instead.
    worker(config, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "copy_chunk", "occurrence": 2, "copy": True})
    assert source.exists() and not plan.entries[0].destination.exists()
    _, errors = recover(config)
    assert not errors
    assert plan.entries[0].destination.read_bytes() == b"movie-data" * 256000


def test_killed_rollback_can_finish(config, tmp_path, monkeypatch):
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    worker(config, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "published",
                             "occurrence": 2, "rollback": True})
    _, errors = recover(config)
    assert not errors, errors
    assert source.read_bytes() == b"movie-data" * 256000 and sidecar.read_bytes() == b"subtitle-data"
    assert not plan.entries[0].destination.exists()


def test_recovery_refuses_replaced_destination(config, tmp_path, monkeypatch):
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    worker(config, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "source_removed"})
    target = plan.entries[0].destination.with_suffix(".en.srt")
    target.write_bytes(b"new subtitle")
    counts, errors = recover(config)
    assert counts["BLOCKED"] == 1 and errors
    assert target.read_bytes() == b"new subtitle" and source.exists()


def test_recovery_refuses_configuration_root_changes(config, tmp_path, monkeypatch):
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    worker(config, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "intent_written"})
    config.movies.library = tmp_path / "other"
    counts, errors = recover(config)
    assert counts["BLOCKED"] == 1 and errors
    assert source.exists() and sidecar.exists()


@pytest.mark.parametrize('stage,occurrence', [('published', 2), ('rollback_original_committed', 1)])
def test_killed_undo_rollback_can_retry_with_changed_inode(config, tmp_path, monkeypatch, stage, occurrence):
    from jellyorganize.filesystem.undo import undo_transaction
    source, sidecar, plan = preparation(config, tmp_path, monkeypatch)
    transaction, _ = apply_plan(plan, config, config.state_dir / 'transactions')
    target = plan.entries[0].destination
    with target.open('rb'):
        worker(config, tmp_path, {'plan_id': plan.plan_id, 'operation': 'undo', 'stage': stage,
                                 'occurrence': occurrence, 'copy': True, 'rollback': True,
                                 'transaction_id': transaction.data['transaction_id']})
    _, errors = recover(config)
    assert not errors, errors
    assert target.read_bytes() == b'movie-data' * 256000 and not source.exists()
    _, counts = undo_transaction(transaction.data['transaction_id'], config)
    assert counts['UNDONE'] == 1
    assert source.read_bytes() == b'movie-data' * 256000 and sidecar.read_bytes() == b'subtitle-data'
