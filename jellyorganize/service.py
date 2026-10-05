"""Generate a user systemd timer and read scheduled-job credentials safely."""

import os
import stat
import sys
from pathlib import Path

from jellyorganize.config import config_path


def token(config):
    value = os.environ.get("TMDB_API_TOKEN", "")
    if value:
        return value
    path = config.service.credential_file
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077 or current.st_uid != os.getuid():
            raise ValueError("credential file must be owned by the current user and readable only by that user (chmod 600)")
        return stream.read().strip()


def _quote(value):
    value = str(value)
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError("service paths cannot contain line breaks or NUL")
    return '"' + value.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"') + '"'


def units(config, path=None, executable=None):
    path = (path or config_path()).expanduser().absolute()
    executable = executable or sys.executable
    return {
        "jellyorganize.service": f"""[Unit]
Description=Organize completed Jellyfin downloads in Incoming
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=oneshot
ExecStart={_quote(executable)} -m jellyorganize.cli --config {_quote(path)} run
SuccessExitStatus=2 3
Restart=on-failure
RestartSec=60
UMask=0077
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal
""",
        "jellyorganize.timer": f"""[Unit]
Description=Schedule Incoming organization

[Timer]
OnBootSec=60
OnUnitInactiveSec={config.service.interval_seconds}
AccuracySec=1
Unit=jellyorganize.service

[Install]
WantedBy=timers.target
""",
    }


def save_credential(config):
    value = token(config)
    if not value:
        raise ValueError("No TMDb credential is available; run `jellyorganize credentials tmdb` or set TMDB_API_TOKEN")
    path = config.service.credential_file
    if path.exists():
        # token() has already checked a file when the environment was absent.
        # Explicitly verify existing ownership/permissions even with an env token.
        current = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077 or current.st_uid != os.getuid():
            raise ValueError("existing credential file must be private and owned by the current user")
        if path.read_text().strip() != value:
            raise FileExistsError("existing credential differs; it was not overwritten")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    from jellyorganize.filesystem.transaction import sync_directory
    sync_directory(path.parent)
    return path


def install(config, path=None, directory=None, *, save_token=False):
    path = (path or config_path()).expanduser().absolute()
    if not path.is_file():
        raise ValueError("create the configuration with `jellyorganize config init` before installing the service")
    directory = directory or Path.home() / ".config/systemd/user"
    content = units(config, path)
    # Check both files before creating either; never replace unrelated units.
    for name, text in content.items():
        target = directory / name
        if target.is_symlink() or (target.exists() and target.read_text() != text):
            raise FileExistsError(f"existing unit differs: {target}; move it aside before reinstalling")
    if save_token:
        save_credential(config)
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in content.items():
        target = directory / name
        if not target.exists():
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
    return directory
