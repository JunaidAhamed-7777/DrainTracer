"""Committed runtime snapshot validation and missing-file discovery."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import main as cli

MANIFEST = "runtime-manifest.json"
CORE_FILES = {
    MANIFEST,
    "src/main.py",
    "src/__init__.py",
    "requirements.txt",
    "TITLE.txt",
    "leak_reports/frames/report.css",
    "leak_reports/frames/report.js",
}


def _write_tree(
    root: Path,
    extra_files: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
) -> dict[str, object]:
    required = sorted(CORE_FILES | set(extra_files))
    for name in required:
        if name == MANIFEST or name in missing:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    document: dict[str, object] = {"schema": 1, "required_files": required}
    (root / MANIFEST).write_text(json.dumps(document), encoding="utf-8")
    return document


class InstallationSnapshotTests(unittest.TestCase):
    def test_complete_tree_does_not_require_generated_reports_or_user_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root)
            self.assertEqual(cli._installation_status(root), ((), None))
            self.assertFalse((root / "leak_reports/reports").exists())
            self.assertFalse((root / ".drainrc").exists())

    def test_missing_manifest_is_reported_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                cli._installation_status(Path(temporary)),
                ((MANIFEST,), None),
            )

    def test_missing_files_are_sorted_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = ("src/zeta.py", "TITLE.txt", "src/alpha.py")
            _write_tree(root, extra_files=("src/zeta.py", "src/alpha.py"), missing=missing)
            self.assertEqual(cli._installation_status(root), (tuple(sorted(missing)), None))

    def test_directory_in_place_of_file_is_reported_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root, missing=("leak_reports/frames/report.css",))
            replacement = root / "leak_reports/frames/report.css"
            replacement.mkdir()
            marker = replacement / "user-data.txt"
            marker.write_text("preserve me", encoding="utf-8")
            self.assertEqual(
                cli._installation_status(root),
                (("leak_reports/frames/report.css",), None),
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")

    def test_checks_installation_root_instead_of_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root)
            with mock.patch.object(Path, "cwd", side_effect=AssertionError("unexpected cwd lookup")):
                self.assertEqual(cli._installation_status(root), ((), None))

    def test_malformed_json_and_invalid_utf8_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for content in (b"", b"{not JSON}", b"\xff"):
                with self.subTest(content=content):
                    (root / MANIFEST).write_bytes(content)
                    _missing, error = cli._installation_status(root)
                    self.assertIsInstance(error, str)
                    self.assertTrue(error)

    def test_invalid_document_shapes_and_schemas_are_errors(self) -> None:
        documents = (
            None,
            [],
            {},
            {"schema": 2, "required_files": sorted(CORE_FILES)},
            {"schema": True, "required_files": sorted(CORE_FILES)},
            {"schema": 1, "required_files": "src/main.py"},
            {"schema": 1, "required_files": {}},
            {"schema": 1, "required_files": []},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for document in documents:
                with self.subTest(document=document):
                    (root / MANIFEST).write_text(json.dumps(document), encoding="utf-8")
                    _missing, error = cli._installation_status(root)
                    self.assertIsInstance(error, str)
                    self.assertTrue(error)

    def test_invalid_entries_cannot_escape_the_installation(self) -> None:
        entries = (
            None,
            42,
            True,
            {},
            "",
            "..",
            "../outside.py",
            "src/../outside.py",
            "/outside.py",
            "//server/share/outside.py",
            "\\outside.py",
            "src\\outside.py",
            "C:/outside.py",
            "C:outside.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for entry in entries:
                with self.subTest(entry=entry):
                    document = {"schema": 1, "required_files": [*sorted(CORE_FILES), entry]}
                    (root / MANIFEST).write_text(json.dumps(document), encoding="utf-8")
                    _missing, error = cli._installation_status(root)
                    self.assertIsInstance(error, str)
                    self.assertTrue(error)

    def test_duplicate_entries_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = {
                "schema": 1,
                "required_files": [*sorted(CORE_FILES), "src/main.py"],
            }
            (root / MANIFEST).write_text(json.dumps(document), encoding="utf-8")
            _missing, error = cli._installation_status(root)
            self.assertIsInstance(error, str)
            self.assertTrue(error)

    def test_omitting_any_core_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for omitted in sorted(CORE_FILES):
                with self.subTest(omitted=omitted):
                    document = {"schema": 1, "required_files": sorted(CORE_FILES - {omitted})}
                    (root / MANIFEST).write_text(json.dumps(document), encoding="utf-8")
                    _missing, error = cli._installation_status(root)
                    self.assertIsInstance(error, str)
                    self.assertTrue(error)

    def test_symlink_outside_installation_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "installation"
            root.mkdir()
            _write_tree(root, extra_files=("linked/data.txt",), missing=("linked/data.txt",))
            outside = workspace / "outside"
            outside.mkdir()
            (outside / "data.txt").write_text("outside data", encoding="utf-8")
            try:
                os.symlink(outside, root / "linked", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            _missing, error = cli._installation_status(root)
            self.assertIsInstance(error, str)
            self.assertTrue(error)

    def test_committed_snapshot_covers_all_runtime_modules_and_assets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        document = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        source_files = {path.relative_to(root).as_posix() for path in (root / "src").glob("*.py")}
        self.assertEqual(document["schema"], 1)
        self.assertEqual(set(document["required_files"]), CORE_FILES | source_files)
        self.assertEqual(len(document["required_files"]), len(set(document["required_files"])))
        self.assertEqual(cli._installation_status(root), ((), None))


if __name__ == "__main__":
    unittest.main()
