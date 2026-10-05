"""Private credential files shared by manual commands and download hooks."""

import os
import stat


def read_private(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ''
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077 or current.st_uid != os.getuid():
            raise ValueError('credential file must be owned by the current user and readable only by that user (chmod 600)')
        return stream.read().strip()


def write_private(path, value):
    value = value.strip()
    if not value or '\x00' in value or '\n' in value or '\r' in value:
        raise ValueError('credential must be a nonempty single line')
    if path.exists() or path.is_symlink():
        previous = read_private(path)
        if previous != value:
            raise FileExistsError('existing credential differs; it was not overwritten')
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    from jellyorganize.filesystem.transaction import sync_directory
    sync_directory(path.parent)
    return path
