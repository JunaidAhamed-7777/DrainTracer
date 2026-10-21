"""Locate read-only assets in source, editable, and wheel installations."""

from __future__ import annotations

from pathlib import Path


def resource_path(relative: str) -> Path:
    """Use the module's layout, never the caller's working directory.

    Setuptools maps the checkout's ``src`` directory to ``draintracer`` in
    a wheel. Editable installs continue using that original source directory.
    """
    package = Path(__file__).resolve().parent
    root = package.parent if package.name == "src" else package / "_data"
    return root / relative
