"""Probe configured roots with disposable files, never with library media."""

import os
import secrets
import stat
from contextlib import ExitStack

from jellyorganize.filesystem.safety import root_fd


def probe(source, destination, mode):
    result = {"source": str(source), "destination": str(destination), "status": "OK",
              "link_supported": False}
    names = []
    try:
        with ExitStack() as stack:
            src = root_fd(source)
            stack.callback(os.close, src)
            dst = root_fd(destination)
            stack.callback(os.close, dst)
            source_name = ".jellyorganize-doctor-" + secrets.token_hex(16)
            destination_name = ".jellyorganize-doctor-" + secrets.token_hex(16)
            def cleanup():
                for parent, name, identity in reversed(names):
                    try:
                        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == identity:
                            os.unlink(name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            stack.callback(cleanup)
            descriptor = os.open(source_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=src)
            current = os.fstat(descriptor)
            names.append((src, source_name, (current.st_dev, current.st_ino)))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"jellyorganize filesystem check\n")
                stream.flush()
                os.fsync(stream.fileno())
            result.update(source_device=current.st_dev, destination_device=os.fstat(dst).st_dev)
            try:
                if mode == "symlink":
                    os.symlink(str(source / source_name), destination_name, dir_fd=dst)
                else:
                    os.link(source_name, destination_name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
                linked = os.stat(destination_name, dir_fd=dst, follow_symlinks=False)
                names.append((dst, destination_name, (linked.st_dev, linked.st_ino)))
                if mode == "symlink":
                    target = os.stat(destination_name, dir_fd=dst)
                    result["link_supported"] = (stat.S_ISLNK(linked.st_mode) and
                                                os.readlink(destination_name, dir_fd=dst) == str(source / source_name) and
                                                (target.st_dev, target.st_ino) == (current.st_dev, current.st_ino))
                else:
                    result["link_supported"] = (linked.st_dev, linked.st_ino) == (current.st_dev, current.st_ino)
                if not result["link_supported"]:
                    raise ValueError("link identity verification failed")
                os.fsync(dst)
            except OSError as error:
                result["link_error"] = f"{error.strerror or type(error).__name__} (errno {error.errno})"
                if mode in {"hardlink", "symlink"}:
                    raise ValueError(mode + " probe failed: " + result["link_error"]) from error
                descriptor = os.open(destination_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=dst)
                current = os.fstat(descriptor)
                names.append((dst, destination_name, (current.st_dev, current.st_ino)))
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(b"jellyorganize destination check\n")
                    stream.flush()
                    os.fsync(stream.fileno())
    except (OSError, ValueError) as error:
        result.update(status="ERROR", error=str(error))
    return result


def check(config):
    stages = [("Incoming → movies", config.incoming_root("movie"), config.movies.library),
              ("Incoming → TV", config.incoming_root("tv"), config.tv.library)]
    if config.downloads.path is not None and config.incoming.path is not None:
        stages.insert(0, ("Downloads → Incoming", config.downloads.path, config.incoming.path))
    results = [{"stage": label, **probe(source, destination, config.filesystem.mode)}
               for label, source, destination in stages]
    return {"mode": config.filesystem.mode, "passed": all(row["status"] == "OK" for row in results),
            "checks": results}
