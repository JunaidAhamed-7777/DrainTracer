"""The runtime version, release notes, changelog and README series agree."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def runtime_version() -> str:
    """Read DashboardApp.VERSION without importing runtime dependencies."""
    tree = ast.parse((ROOT / "src" / "dashboard.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DashboardApp":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "VERSION"
                    for target in statement.targets
                ):
                    return ast.literal_eval(statement.value)
    raise AssertionError("src/dashboard.py does not define DashboardApp.VERSION")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def newest_entry(text: str) -> str:
    """Return the version of the first `## vX.Y.Z` heading."""
    match = re.search(r"^## (v\S+)", text, re.MULTILINE)
    if match is None:
        raise AssertionError("no '## vX.Y.Z' release entry found")
    return match.group(1)


class ReleaseConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.version = runtime_version()
        self.assertRegex(self.version, rf"^{VERSION.pattern}$")

    def test_releases_current_version_matches_runtime(self) -> None:
        first_line = read("RELEASES.md").splitlines()[0]
        self.assertEqual(first_line, f"# Current version: {self.version}")

    def test_releases_newest_entry_matches_runtime(self) -> None:
        self.assertEqual(newest_entry(read("RELEASES.md")), self.version)

    def test_changelog_newest_entry_matches_runtime(self) -> None:
        self.assertEqual(newest_entry(read("CHANGELOG.md")), self.version)

    def test_readme_release_series_matches_major_minor(self) -> None:
        major, minor, _patch = VERSION.fullmatch(self.version).groups()
        series = re.findall(r"Release series: (v[0-9.]+)", read("README.md"))
        self.assertEqual(series, [f"v{major}.{minor}.0"])


if __name__ == "__main__":
    unittest.main()
