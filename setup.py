"""Build the installed layout from the committed source runtime snapshot."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.errors import SetupError

ROOT = Path(__file__).resolve().parent


def runtime_version() -> str:
    """Read the canonical version without importing optional runtime packages."""
    tree = ast.parse((ROOT / "src/dashboard.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DashboardApp":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "VERSION"
                    for target in statement.targets
                ):
                    value = ast.literal_eval(statement.value)
                    if isinstance(value, str) and re.fullmatch(r"v\d+\.\d+\.\d+", value):
                        return value[1:]
    raise SetupError("src/dashboard.py must define DashboardApp.VERSION as vMAJOR.MINOR.PATCH")


def runtime_files() -> list[str]:
    """Fail an incomplete build rather than inferring a smaller inventory."""
    snapshot = json.loads((ROOT / "runtime-manifest.json").read_text(encoding="utf-8"))
    required = snapshot.get("required_files")
    if (type(snapshot.get("schema")) is not int or snapshot["schema"] != 1
            or not isinstance(required, list) or not required):
        raise SetupError("Invalid committed runtime-manifest.json")
    for relative in required:
        if (not isinstance(relative, str) or not relative
                or any(character in relative for character in "\\:\x00")
                or any(ord(character) < 32 for character in relative)
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise SetupError("Unsafe path in runtime-manifest.json")
        source = ROOT / relative
        if not source.resolve().is_relative_to(ROOT) or not source.is_file():
            raise SetupError(f"Missing or unsafe required runtime file: {relative}")
    if len(set(required)) != len(required):
        raise SetupError("Duplicate path in runtime-manifest.json")
    return required


class BuildRuntime(build_py):
    """Keep shared assets inside the package, away from generated reports."""

    def run(self) -> None:
        required = runtime_files()
        super().run()
        for relative in required:
            if relative.startswith("src/"):
                continue
            destination = Path(self.build_lib) / "draintracer" / "_data" / relative
            self.mkpath(str(destination.parent))
            self.copy_file(str(ROOT / relative), str(destination))

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        assets = [
            str(Path(self.build_lib) / "draintracer" / "_data" / relative)
            for relative in runtime_files()
            if not relative.startswith("src/")
        ]
        return [*super().get_outputs(include_bytecode), *assets]


# Validate source distributions and metadata preparation as well as wheels.
runtime_files()
setup(version=runtime_version(), cmdclass={"build_py": BuildRuntime})
