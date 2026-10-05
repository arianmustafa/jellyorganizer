"""Values passed between scanning, providers, matching, and planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator


MediaKind = Literal["movie", "tv"]
Status = Literal["CONFIRMED", "REVIEW", "SKIP", "CONFLICT", "ERROR"]


@dataclass
class MediaItem:
    path: Path
    kind: MediaKind
    root: Path
    sidecars: list[Path] = field(default_factory=list)
    hints: dict = field(default_factory=dict)
    reason: str | None = None


@dataclass
class ScanResult:
    items: list[MediaItem] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    unassociated: list[Path] = field(default_factory=list)


class Candidate(BaseModel):
    provider: str
    provider_id: str
    kind: MediaKind
    title: str
    year: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    tvmaze_id: str | None = None

    @field_validator("provider_id")
    @classmethod
    def numeric_provider_id(cls, value: str) -> str:
        if not value.isdecimal() or int(value) <= 0:
            raise ValueError("provider ID must be a positive decimal number")
        return value

    @field_validator("tvmaze_id")
    @classmethod
    def numeric_tvmaze_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.isdecimal() or int(value) <= 0):
            raise ValueError("TVmaze ID must be a positive decimal number")
        return value


@dataclass
class Proposal:
    item: MediaItem
    status: Status
    reason: str
    confidence: float = 0.0
    candidate: Candidate | None = None
    destination: Path | None = None
    sidecar_destinations: dict[Path, Path] = field(default_factory=dict)
    alternatives: list[Candidate] = field(default_factory=list)
