"""Optional, retryable notifications and targeted Jellyfin library updates."""

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from jellyorganize import __version__
from jellyorganize.credentials import read_private
from jellyorganize.filesystem.locking import media_lock
from jellyorganize.filesystem.transaction import Transaction
from jellyorganize.operations import Operations


def refresh_settings(config):
    if not config.jellyfin.enabled:
        return None
    return {"server": config.jellyfin.url, "roots": {
        "movie": {"local": str(config.movies.library),
                  "server": str(config.jellyfin.movies_path or config.movies.library)},
        "tv": {"local": str(config.tv.library),
               "server": str(config.jellyfin.tv_path or config.tv.library)}}}


def queue_refreshes(config, operations):
    # Journals retain the opt-in and mount mapping from apply time. Reading them
    # also covers a crash between media commit and recording the run summary.
    with media_lock(config.state_dir / "transactions"):
        with operations.database() as connection:
            for path in sorted((config.state_dir / "transactions").glob("*.json")):
                transaction = Transaction.load(path.parent, path.stem)
                settings = transaction.data.get("jellyfin_refresh")
                if not settings:
                    continue
                for index, item in enumerate(transaction.data["items"]):
                    if (item.get("status") != "applied" or item.get("operation") not in {"ingest", "audit"}
                            or item.get("kind") not in settings["roots"]):
                        continue
                    root = settings["roots"][item["kind"]]
                    updates = []
                    for file in item.get("files", []):
                        if file.get("status") != "moved" or file.get("reused"):
                            continue
                        relative = Path(file["destination"]).relative_to(root["local"])
                        updates.append({"Path": str(Path(root["server"]) / relative), "UpdateType": "Created"})
                        if item["operation"] == "audit":
                            relative = Path(file["source"]).relative_to(root["local"])
                            updates.append({"Path": str(Path(root["server"]) / relative), "UpdateType": "Deleted"})
                    if updates:
                        event_id = f"jellyfin:{transaction.data['transaction_id']}:{index}"
                        operations.enqueue(connection, event_id, "jellyfin",
                                           {"server": settings["server"], "Updates": updates})


def secret(path, environment):
    value = os.environ.get(environment) or read_private(path)
    if not value:
        raise ValueError(f"missing credential: {environment} or private credential file")
    return value


def webhook_url(config):
    value = secret(config.notifications.webhook_url_file, "JELLYORGANIZE_WEBHOOK_URL")
    try:
        parsed = urlsplit(value)
        parsed.port
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL) as error:
        raise ValueError("webhook credential is not a valid HTTP(S) URL") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or
            parsed.password is not None or parsed.fragment or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise ValueError("webhook credential must be an HTTP(S) URL without embedded username or password")
    return value


async def flush(config, *, transport=None, limit=20):
    operations = Operations(config.state_dir / "operations.sqlite3")
    result = {"sent": 0, "failed": 0, "pending": 0, "errors": []}
    try:
        with media_lock(config.state_dir / "integration-delivery"):
            try:
                queue_refreshes(config, operations)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                # A broken media journal must not suppress its failure alert.
                result["failed"] += 1
                result["errors"].append("refresh journal processing: " + type(error).__name__)
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, transport=transport) as client:
                attempted = 0
                failed_channels = set()
                for delivery in operations.pending_deliveries():
                    channel, payload = delivery["channel"], delivery["payload"]
                    enabled = config.notifications.enabled if channel == "webhook" else config.jellyfin.enabled
                    if not enabled or channel in failed_channels or attempted >= limit:
                        continue
                    if channel == "jellyfin" and payload["server"] != config.jellyfin.url:
                        message = "pending refresh belongs to a different Jellyfin server"
                        operations.delivery_result(delivery["event_id"], message)
                        result["failed"] += 1
                        result["errors"].append(f"{channel}: {message}")
                        continue
                    attempted += 1
                    try:
                        if channel == "webhook":
                            url = webhook_url(config)
                            headers = {"Idempotency-Key": delivery["event_id"]}
                            body = payload
                        else:
                            key = secret(config.jellyfin.api_key_file, "JELLYFIN_API_KEY")
                            if any(ord(char) < 33 or ord(char) > 126 or char in '\\"' for char in key):
                                raise ValueError("Jellyfin API key contains invalid characters")
                            url = config.jellyfin.url + "/Library/Media/Updated"
                            headers = {"Authorization": 'MediaBrowser Client="Jellyorganize", Device="CLI", '
                                       f'DeviceId="jellyorganize", Version="{__version__}", Token="{key}"'}
                            body = {"Updates": payload["Updates"]}
                        response = await client.post(url, headers=headers, json=body)
                        if not 200 <= response.status_code < 300:
                            raise ValueError(f"HTTP {response.status_code}")
                    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as error:
                        # HTTP exceptions can include credential-bearing URLs;
                        # store only our messages or the exception type.
                        message = str(error) if type(error) is ValueError else type(error).__name__
                        operations.delivery_result(delivery["event_id"], message)
                        result["failed"] += 1
                        result["errors"].append(f"{channel}: {message}")
                        failed_channels.add(channel)
                    else:
                        operations.delivery_result(delivery["event_id"])
                        result["sent"] += 1
            result["pending"] = len(operations.pending_deliveries())
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result["failed"] += 1
        result["errors"].append("integration processing: " + type(error).__name__)
    return result
