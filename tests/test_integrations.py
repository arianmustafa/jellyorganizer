import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from jellyorganize import cli, integrations
from jellyorganize.config import Config
from jellyorganize.filesystem.apply import apply_plan
from jellyorganize.filesystem.recovery import recover
from jellyorganize.operations import Operations
from jellyorganize.planning.store import PlanStore
from jellyorganize.planning.audit import plan_audit
from jellyorganize.scanner.library import scan_library
from test_apply import confirmed_plan, touch
from test_auto import setup
from test_planning import FakeTMDb
from test_recovery import preparation, worker


@pytest.fixture
def enabled(config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("JELLYFIN_API_KEY", "synthetic-private-key")
    monkeypatch.setenv("JELLYORGANIZE_WEBHOOK_URL", "https://example.test/hooks/private-secret")
    config.notifications.enabled = True
    config.jellyfin.enabled = True
    config.jellyfin.url = "https://jellyfin.test/proxy"
    config.jellyfin.movies_path = Path("/media/movies")
    return config


def pending(config):
    return Operations(config.state_dir / "operations.sqlite3").pending_deliveries()


def dispatch(config, handler):
    return asyncio.run(integrations.flush(config, transport=httpx.MockTransport(handler)))


def problem_plan(config):
    source = config.movies.incoming / "Dune.2021.mkv"
    touch(source)
    plan = confirmed_plan(config, config.state_dir, source)
    plan.entries[0].status = "REVIEW"
    plan.entries[0].reason = "ambiguous title"
    return plan


def test_only_new_or_changed_exceptions_notify(enabled):
    plan = problem_plan(enabled)
    operations = Operations(enabled.state_dir / "operations.sqlite3")
    summary = operations.record(plan, 2, config=enabled)
    assert summary["changed_exceptions"] == 1
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(204)
    assert dispatch(enabled, handle)["sent"] == 1
    payload = json.loads(requests[0].content)
    assert payload["event"] == "exceptions"
    assert payload["exceptions"][0]["reason"] == "ambiguous title"
    assert requests[0].headers["Idempotency-Key"] == payload["event_id"]
    second = plan.model_copy(deep=True, update={"plan_id": "20261010-000001-12345678"})
    assert operations.record(second, 2, config=enabled)["changed_exceptions"] == 0
    assert dispatch(enabled, handle)["sent"] == 0
    second.plan_id = "20261010-000002-12345678"
    second.entries[0].reason = "year differs"
    assert operations.record(second, 2, config=enabled)["changed_exceptions"] == 1
    assert dispatch(enabled, handle)["sent"] == 1
    assert len(requests) == 2
    second.entries[0].reason = "different failure on the same saved plan"
    operations.record(second, 2, config=enabled)
    assert dispatch(enabled, handle)["sent"] == 1


def test_disabled_channel_pauses_pending_deliveries(enabled):
    plan = problem_plan(enabled)
    Operations(enabled.state_dir / "operations.sqlite3").record(plan, 2, config=enabled)
    enabled.notifications.enabled = False
    assert dispatch(enabled, lambda request: pytest.fail("disabled channel sent"))["pending"] == 1
    enabled.notifications.enabled = True
    assert dispatch(enabled, lambda request: httpx.Response(204))["sent"] == 1


def test_failed_channel_does_not_block_other_integration(enabled, tmp_path, monkeypatch):
    _, _, plan = preparation(enabled, tmp_path, monkeypatch)
    apply_plan(plan, enabled, enabled.state_dir / "transactions")
    Operations(enabled.state_dir / "operations.sqlite3").failure("new failure", config=enabled)
    def handle(request):
        return httpx.Response(503 if request.url.host == "example.test" else 204)
    result = dispatch(enabled, handle)
    assert result["failed"] == result["sent"] == result["pending"] == 1


def test_audit_refresh_includes_old_and_new_paths(enabled):
    source = enabled.movies.library / "Dune (2021) [tmdbid-438631]" / "Dune.2021.mkv"
    touch(source)
    proposals = asyncio.run(plan_audit(scan_library(enabled, "movie"), enabled, FakeTMDb()))
    plan = PlanStore(enabled.state_dir / "plans").create(proposals, enabled, workflow="audit")
    _, counts = apply_plan(plan, enabled, enabled.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(204)
    assert dispatch(enabled, handle)["sent"] == 1
    assert {row["UpdateType"] for row in requests[0]["Updates"]} == {"Created", "Deleted"}
    assert {row["Path"] for row in requests[0]["Updates"]} == {
        "/media/movies/Dune (2021) [tmdbid-438631]/Dune.2021.mkv",
        "/media/movies/Dune (2021) [tmdbid-438631]/Dune (2021).mkv"}


def test_failed_webhook_retries_without_secret_leaks(enabled):
    plan = problem_plan(enabled)
    operations = Operations(enabled.state_dir / "operations.sqlite3")
    operations.record(plan, 2, config=enabled)
    def timeout(request):
        raise httpx.ReadTimeout("private-secret", request=request)
    result = dispatch(enabled, timeout)
    assert result["failed"] == result["pending"] == 1
    assert "private-secret" not in json.dumps(result)
    assert pending(enabled)[0]["attempts"] == 1
    assert pending(enabled)[0]["last_error"] == "ReadTimeout"
    assert "private-secret" not in operations.path.read_bytes().decode(errors="ignore")
    event_id = pending(enabled)[0]["event_id"]
    requests = []
    def success(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200)
    assert dispatch(enabled, success)["sent"] == 1
    assert requests[0]["event_id"] == event_id
    assert pending(enabled) == []


def test_repeated_identical_fatal_failure_deduplicated(enabled):
    operations = Operations(enabled.state_dir / "operations.sqlite3")
    operations.failure("Incoming unavailable", config=enabled)
    operations.failure("Incoming unavailable", config=enabled)
    assert len(pending(enabled)) == 1
    operations.failure("different failure", config=enabled)
    assert len(pending(enabled)) == 2


def test_failure_alert_still_delivers_when_media_journal_is_corrupt(enabled):
    root = enabled.state_dir / "transactions"
    root.mkdir(parents=True)
    (root / "20261010-000000-12345678.json").write_text("broken journal")
    Operations(enabled.state_dir / "operations.sqlite3").failure("recovery needs attention", config=enabled)
    result = dispatch(enabled, lambda request: httpx.Response(204))
    assert result["sent"] == result["failed"] == 1
    assert result["pending"] == 0


def test_targeted_jellyfin_refresh_maps_paths_and_deduplicates(enabled, tmp_path, monkeypatch):
    _, _, plan = preparation(enabled, tmp_path, monkeypatch)
    transaction, counts = apply_plan(plan, enabled, enabled.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.path == "/proxy/Library/Media/Updated"
        assert request.headers["Authorization"].startswith("MediaBrowser ")
        assert 'Token="synthetic-private-key"' in request.headers["Authorization"]
        assert "synthetic-private-key" not in str(request.url)
        return httpx.Response(204)
    assert dispatch(enabled, handle)["sent"] == 1
    paths = {row["Path"] for row in json.loads(requests[0].content)["Updates"]}
    assert paths == {str(Path("/media/movies") / file.destination.relative_to(enabled.movies.library))
                     for file in plan.entries[0].files}
    assert dispatch(enabled, handle)["sent"] == 0
    assert len(requests) == 1
    assert "synthetic-private-key" not in transaction.path.read_text()


def test_refresh_survives_process_crash_before_run_summary(enabled, tmp_path, monkeypatch):
    _, _, plan = preparation(enabled, tmp_path, monkeypatch)
    worker(enabled, tmp_path, {"plan_id": plan.plan_id, "operation": "apply", "stage": "item_committed"})
    assert not (enabled.state_dir / "operations.sqlite3").exists()
    _, errors = recover(enabled)
    assert not errors
    assert dispatch(enabled, lambda request: httpx.Response(204))["sent"] == 1
    assert dispatch(enabled, lambda request: pytest.fail("duplicate refresh"))["sent"] == 0


def test_server_outage_retains_refresh_and_does_not_affect_media(enabled, tmp_path, monkeypatch):
    _, _, plan = preparation(enabled, tmp_path, monkeypatch)
    transaction, counts = apply_plan(plan, enabled, enabled.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    result = dispatch(enabled, lambda request: httpx.Response(503, text="secret response body"))
    assert result["failed"] == result["pending"] == 1
    assert result["errors"] == ["jellyfin: HTTP 503"]
    assert plan.entries[0].destination.exists()
    assert pending(enabled)[0]["last_error"] == "HTTP 503"
    enabled.jellyfin.url = "https://different.test"
    result = dispatch(enabled, lambda request: pytest.fail("sent old event with credentials for a different server"))
    assert result["failed"] == result["pending"] == 1
    enabled.jellyfin.url = "https://jellyfin.test/proxy"
    assert dispatch(enabled, lambda request: httpx.Response(204))["sent"] == 1


def test_old_server_event_does_not_block_current_server_or_consume_budget(enabled):
    operations = Operations(enabled.state_dir / "operations.sqlite3")
    with operations.database() as connection:
        operations.enqueue(connection, "old-server-event", "jellyfin",
                           {"server": "https://previous.test", "Updates": [{"Path": "/old/movie.mkv", "UpdateType": "Created"}]})
        operations.enqueue(connection, "current-server-event", "jellyfin",
                           {"server": enabled.jellyfin.url, "Updates": [{"Path": "/media/movies/movie.mkv", "UpdateType": "Created"}]})
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(204)
    result = asyncio.run(integrations.flush(enabled, transport=httpx.MockTransport(handle), limit=1))
    assert result["sent"] == result["failed"] == result["pending"] == 1
    assert len(requests) == 1 and requests[0].url.host == "jellyfin.test"
    assert pending(enabled)[0]["event_id"] == "old-server-event"


def test_disabled_integrations_do_not_enqueue_or_retroactively_refresh(config, tmp_path, monkeypatch):
    _, _, plan = preparation(config, tmp_path, monkeypatch)
    transaction, counts = apply_plan(plan, config, config.state_dir / "transactions")
    plan.entries[0].status = "REVIEW"
    Operations(config.state_dir / "operations.sqlite3").record(plan, 2, counts, transaction, config=config)
    config.jellyfin.enabled = True
    config.jellyfin.url = "https://jellyfin.test"
    assert dispatch(config, lambda request: pytest.fail("disabled event delivered"))["sent"] == 0
    assert pending(config) == []


def test_stale_apply_never_refreshes(enabled, tmp_path, monkeypatch):
    source, _, plan = preparation(enabled, tmp_path, monkeypatch)
    source.write_bytes(b"changed")
    _, counts = apply_plan(plan, enabled, enabled.state_dir / "transactions")
    assert counts["STALE"] == 1
    assert dispatch(enabled, lambda request: pytest.fail("stale item delivered"))["sent"] == 0


def test_reused_links_do_not_refresh(enabled, tmp_path, monkeypatch):
    enabled.filesystem.mode = "hardlink"
    _, _, plan = preparation(enabled, tmp_path, monkeypatch)
    apply_plan(plan, enabled, enabled.state_dir / "transactions")
    assert dispatch(enabled, lambda request: httpx.Response(204))["sent"] == 1
    _, counts = apply_plan(plan, enabled, enabled.state_dir / "transactions")
    assert counts["APPLIED"] == 1
    assert dispatch(enabled, lambda request: pytest.fail("reused links refreshed"))["sent"] == 0


@pytest.mark.parametrize("bad_url", ["http://host:private-secret/", "file:///private-secret", "https://user:private-secret@host/"])
def test_bad_webhook_credential_redacted(enabled, monkeypatch, bad_url):
    monkeypatch.setenv("JELLYORGANIZE_WEBHOOK_URL", bad_url)
    plan = problem_plan(enabled)
    Operations(enabled.state_dir / "operations.sqlite3").record(plan, 2, config=enabled)
    result = dispatch(enabled, lambda request: pytest.fail("invalid credential delivered"))
    assert result["failed"] == 1
    assert "private-secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_redirects_and_http_errors_remain_pending(enabled, status):
    plan = problem_plan(enabled)
    Operations(enabled.state_dir / "operations.sqlite3").record(plan, 2, config=enabled)
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://different.test"})
    result = dispatch(enabled, handle)
    assert result["failed"] == result["pending"] == 1 and len(requests) == 1


def test_cli_import_dry_run_never_delivers_but_run_does(tmp_path, monkeypatch, capsys):
    path, roots = setup(tmp_path, monkeypatch)
    with path.open("a") as stream:
        stream.write('[notifications]\nenabled = true\n[jellyfin]\nenabled = true\nurl = "https://jellyfin.test"\n')
    monkeypatch.setenv("JELLYFIN_API_KEY", "synthetic-key")
    monkeypatch.setenv("JELLYORGANIZE_WEBHOOK_URL", "https://webhook.test/private")
    touch(roots["movie_in"] / "We.Live.in.Time.2024.mkv")
    touch(roots["movie_in"] / "Unknown.1999.mkv")
    requests = []
    original = integrations.flush
    def handle(request):
        requests.append(request)
        return httpx.Response(204)
    async def fake_flush(config):
        return await original(config, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(integrations, "flush", fake_flush)
    assert cli.main(["--config", str(path), "organize", "movies", "--dry-run"]) == 2
    assert requests == []
    capsys.readouterr()
    assert cli.main(["--config", str(path), "run", "movies"]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["integrations"]["sent"] == 2
    assert {request.url.host for request in requests} == {"webhook.test", "jellyfin.test"}
    assert cli.main(["--config", str(path), "run", "movies"]) == 2
    assert len(requests) == 2
    capsys.readouterr()
    assert cli.main(["--config", str(path), "integrations", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 0


@pytest.mark.parametrize("provider,environment,section,field", [
    ("jellyfin", "JELLYFIN_API_KEY", "jellyfin", "api_key_file"),
    ("webhook", "JELLYORGANIZE_WEBHOOK_URL", "notifications", "webhook_url_file"),
])
def test_private_integration_credentials(enabled, tmp_path, monkeypatch, provider, environment, section, field):
    target = tmp_path / "private-credential"
    setattr(getattr(enabled, section), field, target)
    monkeypatch.setattr(cli, "load_config", lambda path: enabled)
    assert cli.main(["credentials", provider, "--from-env"]) == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == os.environ[environment]


def test_enabling_jellyfin_requires_safe_base_url():
    with pytest.raises(ValueError):
        Config.model_validate({"jellyfin": {"enabled": True}})
    with pytest.raises(ValueError):
        Config.model_validate({"jellyfin": {"enabled": True, "url": "https://user:secret@host/"}})
