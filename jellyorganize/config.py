"""Configuration. Loading never creates or changes media directories."""

from __future__ import annotations

import tomllib
import hashlib
import os
import warnings
from urllib.parse import urlsplit
from pathlib import Path
from importlib.resources import files
from typing import Literal

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_state_dir
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("paths must be absolute or start with ~")
    return path


class Paths(Section):
    library: Path
    incoming: Path

    @field_validator("library", "incoming", mode="before")
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        return absolute_path(value)


class Naming(Section):
    include_tmdb_id: bool = True
    include_episode_title: bool = True
    movie_versions: bool = False


class Matching(Section):
    auto_apply_threshold: float = Field(default=0.97, ge=0, le=1)
    confirm_exact_movies: bool = True


class Incoming(Section):
    path: Path | None = Field(default_factory=lambda: Path("~/media/Incoming").expanduser().absolute())
    minimum_age_seconds: int = Field(default=60, ge=0)
    ignored_extensions: list[str] = Field(default_factory=lambda: [".part", ".partial", ".tmp", ".crdownload"])
    completion: Literal["stable", "marker", "handoff"] = "stable"
    stability_seconds: int = Field(default=60, ge=0)

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, value: str | Path | None) -> Path | None:
        return absolute_path(value) if value is not None else None


class Scanning(Section):
    ignored_tv_directories: list[str] = Field(default_factory=lambda: ["Featurettes"])


class Providers(Section):
    tmdb: bool = True
    tvmaze: bool = True
    omdb: bool = False

    @model_validator(mode="after")
    def supported(self):
        if not self.tmdb:
            raise ValueError("TMDb is required; providers.tmdb = false is unsupported")
        if self.omdb:
            raise ValueError("OMDb is not implemented; providers.omdb = true is unsupported")
        return self


class Filesystem(Section):
    mode: Literal["move", "hardlink"] = "move"
    verify_cross_filesystem_copy: bool = True
    hash_algorithm: str = "sha256"

    @model_validator(mode="after")
    def verified_copies(self):
        if not self.verify_cross_filesystem_copy:
            raise ValueError("cross-filesystem verification cannot be disabled")
        if self.hash_algorithm not in {"sha256", "sha512", "blake2b"}:
            raise ValueError("hash_algorithm must be sha256, sha512, or blake2b")
        hashlib.new(self.hash_algorithm)
        return self


class Service(Section):
    interval_seconds: int = Field(default=300, ge=30, le=86400)
    credential_file: Path = Field(default_factory=lambda: Path("~/.local/share/jellyorganize/tmdb.token").expanduser())

    @field_validator("credential_file", mode="before")
    @classmethod
    def expand_path(cls, value):
        return absolute_path(value)


class Downloads(Section):
    path: Path | None = None

    @field_validator("path", mode="before")
    @classmethod
    def expand_path(cls, value):
        return absolute_path(value) if value is not None else None


class QBittorrent(Section):
    url: str | None = None
    username: str = ""
    password_file: Path = Field(default_factory=lambda: Path("~/.local/share/jellyorganize/qbittorrent.password").expanduser())
    basic_username: str = ""
    basic_password_file: Path = Field(default_factory=lambda: Path("~/.local/share/jellyorganize/qbittorrent-basic.password").expanduser())

    @field_validator("url")
    @classmethod
    def api_url(cls, value):
        if value is None:
            return value
        if any(ord(character) < 33 or ord(character) == 127 for character in value):
            raise ValueError('qBittorrent URL cannot contain whitespace or control characters')
        parsed = urlsplit(value)
        parsed.port  # Validate a present port before passing the URL to HTTPX.
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("qBittorrent URL must be an HTTP(S) base URL without embedded credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("password_file", "basic_password_file", mode="before")
    @classmethod
    def expand_path(cls, value):
        return absolute_path(value) if value is not None else None


class Notifications(Section):
    enabled: bool = False
    webhook_url_file: Path = Field(default_factory=lambda: Path("~/.local/share/jellyorganize/webhook.url").expanduser())

    @field_validator("webhook_url_file", mode="before")
    @classmethod
    def expand_path(cls, value):
        return absolute_path(value)


class Jellyfin(Section):
    enabled: bool = False
    url: str | None = None
    api_key_file: Path = Field(default_factory=lambda: Path("~/.local/share/jellyorganize/jellyfin.key").expanduser())
    movies_path: Path | None = None
    tv_path: Path | None = None

    @field_validator("url")
    @classmethod
    def api_url(cls, value):
        try:
            return QBittorrent.api_url(value)
        except ValueError as error:
            raise ValueError(str(error).replace("qBittorrent", "Jellyfin")) from error

    @field_validator("api_key_file", "movies_path", "tv_path", mode="before")
    @classmethod
    def expand_path(cls, value):
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def configured_server(self):
        if self.enabled and not self.url:
            raise ValueError("jellyfin.enabled requires jellyfin.url")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    movies: Paths = Field(default_factory=lambda: Paths(library="~/media/Movies", incoming="~/media/Incoming/Movies"))
    tv: Paths = Field(default_factory=lambda: Paths(library="~/media/TV Shows", incoming="~/media/Incoming/TV Shows"))
    naming: Naming = Field(default_factory=Naming)
    matching: Matching = Field(default_factory=Matching)
    incoming: Incoming = Field(default_factory=Incoming)
    scanning: Scanning = Field(default_factory=Scanning)
    providers: Providers = Field(default_factory=Providers)
    filesystem: Filesystem = Field(default_factory=Filesystem)
    service: Service = Field(default_factory=Service)
    downloads: Downloads = Field(default_factory=Downloads)
    qbittorrent: QBittorrent = Field(default_factory=QBittorrent)
    notifications: Notifications = Field(default_factory=Notifications)
    jellyfin: Jellyfin = Field(default_factory=Jellyfin)
    schema_version: Literal[1] = 1

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_incoming(cls, value: object) -> object:
        if isinstance(value, dict):
            legacy = any(isinstance(value.get(section), dict) and "incoming" in value[section]
                         for section in ("movies", "tv"))
            result = dict(value)
            matching = result.get("matching")
            if isinstance(matching, dict) and "review_threshold" in matching:
                matching = dict(matching)
                matching.pop("review_threshold")
                result["matching"] = matching
                warnings.warn("matching.review_threshold was unused and has been retired; remove it from the configuration", UserWarning)
            for section, default in (("movies", "~/media/Incoming/Movies"),
                                     ("tv", "~/media/Incoming/TV Shows")):
                if isinstance(result.get(section), dict) and "incoming" not in result[section]:
                    result[section] = {**result[section], "incoming": default}
            incoming = result.get("incoming", {})
            if legacy and isinstance(incoming, dict) and "path" not in incoming:
                result["incoming"] = {**incoming, "path": None}
            return result
        return value

    @model_validator(mode="after")
    def separate_roots(self) -> "Config":
        roots = ([self.incoming.path] if self.incoming.path is not None else
                 [self.movies.incoming, self.tv.incoming]) + [self.movies.library, self.tv.library]
        if self.downloads.path is not None:
            roots.append(self.downloads.path)
        normalized = [root.resolve(strict=False) for root in roots]
        for index, left in enumerate(normalized):
            for right in normalized[index + 1:]:
                if left == right or left in right.parents or right in left.parents:
                    raise ValueError("Incoming and library roots must be separate, non-nested directories")
        return self

    def incoming_root(self, kind: str) -> Path:
        return self.incoming.path or (self.movies.incoming if kind == "movie" else self.tv.incoming)

    @property
    def cache_path(self) -> Path:
        return Path(user_cache_dir("jellyorganize")) / "metadata.sqlite3"

    @property
    def identity_path(self) -> Path:
        return Path(user_data_dir("jellyorganize")) / "identities.sqlite3"

    @property
    def state_dir(self) -> Path:
        return Path(user_state_dir("jellyorganize"))


def config_path() -> Path:
    return Path(user_config_dir("jellyorganize")) / "config.toml"


def init_config(path: Path | None = None) -> Path:
    path = (path or config_path()).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    template = files("jellyorganize.resources").joinpath("config.toml").read_text(encoding="utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(template)
        stream.flush()
        os.fsync(stream.fileno())
    from jellyorganize.filesystem.transaction import sync_directory
    sync_directory(path.parent)
    return path


def load_config(path: Path | None = None) -> Config:
    explicit = path is not None
    path = (path or config_path()).expanduser().absolute()
    if not path.exists():
        if explicit:
            raise ValueError(f"configuration file does not exist: {path}")
        return Config()
    with path.open("rb") as stream:
        return Config.model_validate(tomllib.load(stream))
