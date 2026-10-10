"""Immutable saved plans. Apply never resolves metadata again."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jellyorganize.config import Config
from jellyorganize.models import Candidate, Proposal, Status


class SourceState(BaseModel):
    source: Path
    size: int
    mtime_ns: int
    inode: int
    device: int


class FileState(SourceState):
    destination: Path


class PlanEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Path
    kind: str
    source_root: Path
    destination_root: Path
    status: Status
    reason: str
    confidence: float
    candidate: Candidate | None = None
    alternatives: list[Candidate] = Field(default_factory=list)
    destination: Path | None = None
    source_states: list[SourceState] = Field(default_factory=list)
    files: list[FileState] = Field(default_factory=list)


class SavedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    transfer_mode: Literal["move", "hardlink"] = "move"
    workflow: Literal["ingest", "audit", "handoff"] = "ingest"
    plan_id: str
    created_at: str
    scope: list[str]
    source_roots: dict[str, Path]
    destination_roots: dict[str, Path]
    entries: list[PlanEntry]
    download_hash: str | None = None
    downloader_url: str | None = None
    download_source: Path | None = None

    @model_validator(mode="after")
    def supported_mode(self):
        if self.version not in (1, 2):
            raise ValueError("plan ID or version mismatch")
        if (self.version == 1 or self.workflow == "audit") and self.transfer_mode != "move":
            raise ValueError("legacy and audit plans must use move mode")
        return self


def configured_roots(config: Config, kind: str, workflow: str) -> tuple[Path, Path]:
    if workflow == "handoff":
        if kind != "download" or config.downloads.path is None or config.incoming.path is None:
            raise ValueError("download handoff requires downloads.path and a shared incoming.path")
        return config.downloads.path, config.incoming.path
    paths = config.movies if kind == "movie" else config.tv if kind == "tv" else None
    if paths is None or workflow not in {"ingest", "audit"}:
        raise ValueError("unknown media type or workflow in plan")
    return (config.incoming_root(kind) if workflow == "ingest" else paths.library), paths.library


def snapshot(source: Path) -> SourceState:
    stat = source.lstat()
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"source is not a regular file: {source}")
    return SourceState(source=source, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                       inode=stat.st_ino, device=stat.st_dev)


def capture(source: Path, destination: Path) -> FileState:
    return FileState(**snapshot(source).model_dump(), destination=destination)


def entry_from_proposal(proposal: Proposal, config: Config) -> PlanEntry:
    item = proposal.item
    target_root = config.movies.library if item.kind == "movie" else config.tv.library
    files: list[FileState] = []
    source_states: list[SourceState] = []
    status = proposal.status
    reason = proposal.reason
    if proposal.status in ("CONFIRMED", "REVIEW", "CONFLICT"):
        try:
            source_states = [snapshot(item.path), *(snapshot(path) for path in item.sidecars)]
            if proposal.destination is not None:
                files.append(capture(item.path, proposal.destination))
                files.extend(capture(source, destination) for source, destination in proposal.sidecar_destinations.items())
        except (OSError, ValueError) as error:
            status = "ERROR"
            reason = f"source changed during planning: {error}"
            files = []
            source_states = []
    return PlanEntry(source=item.path, kind=item.kind, source_root=item.root, destination_root=target_root,
                     status=status, reason=reason, confidence=proposal.confidence,
                     candidate=proposal.candidate, alternatives=proposal.alternatives,
                     destination=proposal.destination, source_states=source_states, files=files)


class PlanStore:
    def __init__(self, root: Path):
        self.root = root

    def create(self, proposals: list[Proposal], config: Config, scope: tuple[str, ...] | None = None,
               workflow: Literal["ingest", "audit"] = "ingest") -> SavedPlan:
        scope = scope or tuple(sorted({proposal.item.kind for proposal in proposals}))
        plan = SavedPlan(version=2, transfer_mode=config.filesystem.mode if workflow == "ingest" else "move",
                         plan_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4),
                         workflow=workflow,
                         created_at=datetime.now(timezone.utc).isoformat(),
                         scope=list(scope),
                         source_roots={kind: config.incoming_root(kind)
                                       if workflow == "ingest" else (config.movies.library if kind == "movie" else config.tv.library)
                                       for kind in scope},
                         destination_roots={kind: config.movies.library if kind == "movie" else config.tv.library for kind in scope},
                         entries=[entry_from_proposal(proposal, config) for proposal in proposals])
        return self.save(plan)

    def create_handoff(self, states: list[FileState], config: Config, torrent_id: str,
                       downloader_url: str, content_path: Path) -> SavedPlan:
        source_root, destination_root = configured_roots(config, "download", "handoff")
        if not states:
            raise ValueError("handoff plan must contain files")
        plan = SavedPlan(version=2, transfer_mode=config.filesystem.mode,
                         plan_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4),
                         workflow="handoff", created_at=datetime.now(timezone.utc).isoformat(), scope=["download"],
                         source_roots={"download": source_root}, destination_roots={"download": destination_root},
                         download_hash=torrent_id, downloader_url=downloader_url, download_source=content_path,
                         entries=[PlanEntry(source=states[0].source, kind="download", source_root=source_root,
                                            destination_root=destination_root, status="CONFIRMED", confidence=1.0,
                                            reason="qBittorrent confirms complete, stopped download; preserve nested paths",
                                            destination=states[0].destination, files=states,
                                            source_states=[SourceState.model_validate(state.model_dump()) for state in states])])
        return self.save(plan)

    def save(self, plan: SavedPlan) -> SavedPlan:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{plan.plan_id}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(plan.model_dump(mode="json"), stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        path.chmod(0o400)
        from jellyorganize.filesystem.transaction import sync_directory
        sync_directory(self.root)
        return plan

    def load(self, plan_id: str) -> SavedPlan:
        if not plan_id or any(character not in "0123456789abcdef-" for character in plan_id):
            raise ValueError("invalid plan ID")
        path = self.root / f"{plan_id}.json"
        plan = SavedPlan.model_validate_json(path.read_text(encoding="utf-8"))
        if plan.plan_id != plan_id:
            raise ValueError("plan ID or version mismatch")
        return plan
