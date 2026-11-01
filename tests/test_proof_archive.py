"""Malicious ZIP entries never escape or overwrite preserved proof state."""

from __future__ import annotations

import os
import stat
import struct
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import proof_archive

from src.team_security import secure_directory

COMMIT = "a" * 40


class ProofArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None)
        self.addCleanup(self.temp.cleanup)
        self.root = secure_directory(Path(self.temp.name) / "private", create=True)
        self.archive = self.root / "input.zip"

    def archive_with(self, names):
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.comment = COMMIT.encode()
            for name in names:
                entry = zipfile.ZipInfo(name)
                # Preserve hostile raw separators even on Windows, where the
                # ZipInfo constructor otherwise normalizes them before writing.
                entry.filename = entry.orig_filename = name
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, b"fixture")

    def test_valid_git_archive_copy_hash_and_exclusive_extraction(self):
        self.archive_with(["src/example.py", "README.md"])
        copy = self.root / "source.zip"
        expected = proof_archive.sha256(self.archive)
        self.assertEqual(proof_archive.copy_frozen(self.archive, copy, expected), expected)
        output = self.root / "source"
        proof_archive.extract(copy, output, COMMIT)
        self.assertEqual((output / "src/example.py").read_bytes(), b"fixture")
        with self.assertRaises(FileExistsError):
            proof_archive.extract(copy, output, COMMIT)
        self.assertEqual((output / "README.md").read_bytes(), b"fixture")
        with self.assertRaises(FileExistsError):
            proof_archive.copy_frozen(self.archive, copy, expected)

    def test_hash_and_commit_mismatch_preserve_bytes_without_extraction(self):
        self.archive_with(["src/example.py"])
        copy = self.root / "source.zip"
        with self.assertRaisesRegex(ValueError, "hash"):
            proof_archive.copy_frozen(self.archive, copy, "0" * 64)
        self.assertEqual(copy.read_bytes(), self.archive.read_bytes())
        with self.assertRaisesRegex(ValueError, "commit"):
            proof_archive.extract(copy, self.root / "source", "b" * 40)
        self.assertFalse((self.root / "source").exists())

    def test_absolute_parent_windows_device_and_alternate_stream_paths_are_refused_first(self):
        for name in ("../outside", "/absolute", "C:/outside", "a/../../outside", "a\\outside", "a//b",
                     "a/./b", "a:stream", "NUL.py", "NUL .py", "COM1", "LPT¹.log", "a.", "a ", "bad\x01name", "bad\x00name"):
            self.archive_with(["safe.txt", name])
            with self.subTest(name=name), self.assertRaises(ValueError):
                proof_archive.extract(self.archive, self.root / "source", COMMIT)
            self.assertFalse((self.root / "source").exists())
        self.assertFalse((self.root.parent / "outside").exists())

    def test_symlink_special_entry_and_unknown_unix_link_metadata_are_refused(self):
        for mode, extra in ((stat.S_IFLNK, b""), (stat.S_IFIFO, b""), (stat.S_IFREG, b"\x0d\x00\x00\x00")):
            entry = zipfile.ZipInfo("link")
            entry.create_system = 3
            entry.external_attr = (mode | 0o600) << 16
            entry.extra = extra
            self.archive_with([])
            with zipfile.ZipFile(self.archive, "a") as archive:
                archive.writestr(entry, "../outside")
            with self.subTest(mode=mode, extra=extra), self.assertRaises(ValueError):
                proof_archive.extract(self.archive, self.root / "source", COMMIT)
            self.assertFalse((self.root / "source").exists())

    def test_case_aliases_duplicates_and_file_directory_collisions_are_refused(self):
        for names in (["A/file", "a/other"], ["i/file", "ı/other"], ["src", "src/file"], ["src/file", "src"], ["same", "same"]):
            with self.assertWarns(UserWarning) if names == ["same", "same"] else nullcontext():
                self.archive_with(names)
            with self.subTest(names=names), self.assertRaises(ValueError):
                proof_archive.entries(self.archive, COMMIT)

    def test_archive_file_entry_and_uncompressed_bounds_are_enforced(self):
        self.archive_with(["a", "b", "c"])
        with mock.patch.object(proof_archive, "MAX_ENTRIES", 2), self.assertRaises(ValueError):
            proof_archive.entries(self.archive, COMMIT)
        with mock.patch.object(proof_archive, "MAX_ARCHIVE_BYTES", 1), self.assertRaises(ValueError):
            proof_archive.sha256(self.archive)
        with mock.patch.object(proof_archive, "MAX_FILE_BYTES", 3), self.assertRaises(ValueError):
            proof_archive.entries(self.archive, COMMIT)
        with mock.patch.object(proof_archive, "MAX_UNCOMPRESSED_BYTES", 10), self.assertRaises(ValueError):
            proof_archive.entries(self.archive, COMMIT)
        self.assertFalse((self.root / "source").exists())

    def test_inconsistent_directory_count_and_trailing_data_fail_before_extraction(self):
        self.archive_with(["first", "second"])
        original = self.archive.read_bytes()
        forged = bytearray(original)
        end = forged.rfind(b"PK\x05\x06")
        struct.pack_into("<2H", forged, end + 8, 1, 1)
        for raw in (bytes(forged), original + b"trailing", original[:-1]):
            self.archive.write_bytes(raw)
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                proof_archive.extract(self.archive, self.root / "source", COMMIT)
            self.assertFalse((self.root / "source").exists())

    def test_bad_crc_leaves_only_its_failed_private_attempt_without_overwrite_or_cleanup(self):
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.comment = COMMIT.encode()
            archive.writestr("fixture.txt", b"fixture")
        raw = bytearray(self.archive.read_bytes())
        name, extra = struct.unpack_from("<2H", raw, 26)
        raw[30 + name + extra] ^= 1
        self.archive.write_bytes(raw)
        output = self.root / "source"
        with self.assertRaises(zipfile.BadZipFile):
            proof_archive.extract(self.archive, output, COMMIT)
        self.assertTrue((output / "fixture.txt").is_file())
        with self.assertRaises(FileExistsError):
            proof_archive.extract(self.archive, output, COMMIT)


if __name__ == "__main__":
    unittest.main()
