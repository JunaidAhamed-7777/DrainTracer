"""Private local paths and credential-file I/O; no network or global umask changes."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .team_models import TeamError


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def _posix_ancestors(path: Path) -> None:
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError("private storage ancestors must be ordinary directories")
        if info.st_uid not in (0, os.getuid()):
            raise PermissionError("private storage ancestry has an untrusted owner")
        # Root-owned sticky directories such as /tmp protect an owned child
        # from replacement by other users. Non-sticky public writers do not.
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise PermissionError("private storage ancestry permits untrusted mutation")


def secure_directory(value: str | Path, *, create: bool = False) -> Path:
    path = _absolute(value)
    try:
        if os.name == "nt":
            from .agent_transport import private_directory, verify_private_path
            if create:
                path = private_directory(path)
            else:
                verify_private_path(path)
            if not path.is_dir():
                raise PermissionError("private store is not a directory")
        else:
            _posix_ancestors(path.parent)
            if create:
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise PermissionError("store directory must be owned and private")
        return path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as error:
        raise TeamError("storage", "Store path must be a private local directory with trusted ancestors") from error


def verify_data_file(value: str | Path, *, writable: bool = True) -> Path:
    path = _absolute(value)
    try:
        secure_directory(path.parent)
        if os.name == "nt":
            from .agent_transport import verify_private_data_path
            verify_private_data_path(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("private data must be an ordinary single-link file")
        if os.name != "nt":
            needed = 0o600 if writable else 0o400
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_mode & needed != needed:
                raise PermissionError("private data permissions do not match its owner/access policy")
        return path
    except TeamError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise TeamError("storage", "Private data permissions or path could not be verified") from error


@contextmanager
def private_file_lock(path: Path):
    """A cooperative one-byte OS lock, released by close or process termination."""
    secure_directory(path.parent)
    if path.exists() or path.is_symlink():
        verify_data_file(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        verify_data_file(path)
        if os.fstat(descriptor).st_size > 1:
            raise TeamError("storage", "Invalid private lock file")
        if not os.fstat(descriptor).st_size:
            handle.write(b"\0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise TeamError("busy", "Private store administration is busy or unavailable") from error
        yield
    finally:
        handle.close()


def write_credential_file(path: str | Path, value: dict) -> Path:
    destination = _absolute(path)
    secure_directory(destination.parent)
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(encoded) > 8192:
        raise TeamError("invalid", "Credential file exceeds its size limit")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    # An uncertain exclusively-created file remains for local inspection;
    # retries never overwrite it or silently turn a partial write into success.
    with os.fdopen(descriptor, "wb") as handle:
        verify_data_file(destination)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def read_credential_file(path: str | Path) -> dict:
    source = verify_data_file(path, writable=False)
    with source.open("rb") as handle:
        raw = handle.read(8193)
    if len(raw) > 8192:
        raise TeamError("invalid", "Credential file exceeds its size limit")
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise TeamError("invalid", "Credential file contains a duplicate field")
                value[key] = item
            return value
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise TeamError("invalid", "Credential file is not usable JSON") from None
    if (not isinstance(value, dict) or set(value) != {"team_credentials_schema", "project_id", "token"}
            or type(value["team_credentials_schema"]) is not int or value["team_credentials_schema"] != 1
            or not isinstance(value["project_id"], str) or not isinstance(value["token"], str)):
        raise TeamError("invalid", "Credential file has an unsupported schema")
    return value


__all__ = ["private_file_lock", "read_credential_file", "secure_directory", "verify_data_file", "write_credential_file"]
