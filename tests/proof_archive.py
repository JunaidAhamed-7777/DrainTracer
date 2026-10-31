"""Bounded Git ZIP validation and exclusive extraction for preserved proof fixtures."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import struct
import unicodedata
import zipfile

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_CENTRAL_BYTES = 8 * 1024 * 1024
MAX_NAME_BYTES = 512
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])\Z", re.IGNORECASE)


def commit_id(value):
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ValueError("Source commit must be a full lowercase SHA")
    return value


def archive_hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Archive hash must be lowercase SHA256")
    return value


def _ordinary(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > MAX_ARCHIVE_BYTES):
        raise ValueError("Source archive must be an ordinary bounded single-link file")
    return info.st_size


def sha256(path):
    _ordinary(path)
    digest, observed = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(65536), b""):
            observed += len(part)
            if observed > MAX_ARCHIVE_BYTES:
                raise ValueError("Source archive exceeds its byte bound")
            digest.update(part)
    return digest.hexdigest()


def copy_frozen(source, destination, expected_hash):
    """Preserve the selected bytes exclusively, then verify their expected hash."""
    archive_hash(expected_hash)
    _ordinary(source)
    observed, digest = 0, hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for part in iter(lambda: reader.read(65536), b""):
            observed += len(part)
            if observed > MAX_ARCHIVE_BYTES:
                raise ValueError("Source archive exceeds its byte bound")
            writer.write(part)
            digest.update(part)
        writer.flush()
        os.fsync(writer.fileno())
    if digest.hexdigest() != expected_hash:
        raise ValueError("Frozen archive hash does not match the selected trusted bytes")
    return digest.hexdigest()


def _central(path):
    """Bound directory metadata before ZipFile allocates its entry objects."""
    size = _ordinary(path)
    with path.open("rb") as handle:
        handle.seek(max(0, size - 65557))
        tail = handle.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ValueError("Missing ordinary ZIP directory")
        _signature, disk, directory_disk, disk_count, count, length, start, comment = struct.unpack_from("<4s4H2LH", tail, offset)
        end_position = max(0, size - 65557) + offset
        if (disk or directory_disk or disk_count != count or not 1 <= count <= MAX_ENTRIES
                or not 0 < length <= MAX_CENTRAL_BYTES or start + length != end_position
                or offset + 22 + comment != len(tail)):
            raise ValueError("Unsupported, excessive or inconsistent ZIP directory")
        handle.seek(start)
        directory = handle.read(length)
    observed = position = 0
    while position < len(directory):
        if directory[position:position + 4] != b"PK\x01\x02" or len(directory) - position < 46:
            raise ValueError("Invalid ZIP entry directory")
        name, extra, comment = struct.unpack_from("<3H", directory, position + 28)
        if not 0 < name <= MAX_NAME_BYTES:
            raise ValueError("ZIP entry name exceeds its bound")
        position += 46 + name + extra + comment
        observed += 1
        if observed > MAX_ENTRIES or position > len(directory):
            raise ValueError("ZIP directory entry count or length is inconsistent")
    if observed != count:
        raise ValueError("ZIP directory count is inconsistent")
    return start


def _parts(item):
    name = item.orig_filename
    if item.filename != name or "\\" in name or len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError("ZIP path is redirected, truncated or excessive")
    pieces = name[:-1].split("/") if item.is_dir() else name.split("/")
    if not 1 <= len(pieces) <= 32:
        raise ValueError("ZIP path depth exceeds its bound")
    for part in pieces:
        if (not part or part in {".", ".."} or len(part) > 255 or ":" in part or part.endswith((".", " "))
                or _DEVICE.fullmatch(part.split(".", 1)[0])
                or any(unicodedata.category(character).startswith("C") for character in part)):
            raise ValueError("ZIP path is unsafe on supported platforms")
    mode = stat.S_IFMT(item.external_attr >> 16)
    if (mode not in {0, stat.S_IFREG, stat.S_IFDIR} or mode == stat.S_IFDIR and not item.is_dir()
            or mode == stat.S_IFREG and item.is_dir() or item.flag_bits & 1 or item.extract_version > 20
            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
        raise ValueError("ZIP links, special files or unsupported compression are refused")
    position = 0
    while position < len(item.extra):
        if len(item.extra) - position < 4:
            raise ValueError("Invalid ZIP extra metadata")
        tag, length = struct.unpack_from("<2H", item.extra, position)
        position += 4 + length
        # Git archives use extended timestamps. Do not interpret unknown Unix
        # link targets, alternate paths, ZIP64 sizes or unsupported metadata.
        if tag != 0x5455 or position > len(item.extra):
            raise ValueError("Unsupported ZIP extra metadata")
    return tuple(pieces)


def entries(path, source_commit):
    """Validate the WHOLE archive before creating an extraction leaf."""
    commit_id(source_commit)
    central_start = _central(path)
    namespace, explicit, result, total = {}, set(), [], 0
    with zipfile.ZipFile(path) as package:
        if package.comment != source_commit.encode("ascii"):
            raise ValueError("Git archive comment does not match the declared source commit")
        for item in package.infolist():
            parts = _parts(item)
            if item.header_offset < 0 or item.header_offset >= central_start:
                raise ValueError("ZIP local header lies outside the file-entry area")
            for index in range(1, len(parts) + 1):
                original = parts[:index]
                key = tuple(unicodedata.normalize("NFC", part).casefold() for part in original)
                directory = index < len(parts) or item.is_dir()
                previous = namespace.get(key)
                if previous is not None and previous != (original, directory):
                    raise ValueError("ZIP contains platform aliases or file/directory collisions")
                namespace[key] = (original, directory)
            if key in explicit:
                raise ValueError("ZIP contains duplicate paths")
            explicit.add(key)
            if (item.file_size < 0 or item.file_size > MAX_FILE_BYTES
                    or item.is_dir() and item.file_size or item.compress_size < 0):
                raise ValueError("ZIP entry exceeds its file-size bound")
            total += item.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP exceeds its total uncompressed bound")
            result.append((item, parts))
    return result


def _new_directory(path):
    from src.team_security import secure_directory
    secure_directory(path.parent)
    if os.name == "nt":
        from src.agent_transport import _api, _attributes
        kernel, _security = _api()
        with _attributes(inherit_children=True) as attributes:
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
    else:
        path.mkdir(mode=0o700)
    secure_directory(path)


def extract(path, destination, source_commit):
    selected = entries(path, source_commit)
    _new_directory(destination)  # Exclusive; never reuse/delete an earlier leaf.
    total = 0
    with zipfile.ZipFile(path) as package:
        for item, parts in selected:
            target = destination.joinpath(*parts)
            if not target.is_relative_to(destination):
                raise ValueError("ZIP path escaped its new private leaf")
            for parent in reversed(target.parents):
                if parent == destination or destination in parent.parents:
                    if not parent.exists():
                        _new_directory(parent)
                    info = parent.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise ValueError("Extraction path is no longer an ordinary directory")
            if item.is_dir():
                if not target.exists():
                    _new_directory(target)
                continue
            count = 0
            with package.open(item) as reader, target.open("xb") as writer:
                for part in iter(lambda: reader.read(65536), b""):
                    count += len(part)
                    total += len(part)
                    if count > MAX_FILE_BYTES or total > MAX_UNCOMPRESSED_BYTES:
                        raise ValueError("Expanded ZIP data exceeds its bounds")
                    writer.write(part)
            if count != item.file_size:
                raise ValueError("Expanded ZIP entry size does not match its directory")
