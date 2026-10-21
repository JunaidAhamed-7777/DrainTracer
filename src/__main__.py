"""Dependency-free entry point for the installed DrainTracer command."""

from __future__ import annotations

import runpy


def main() -> None:
    """Run the startup check before importing the dashboard or its dependencies."""
    runpy.run_module(f"{__package__}.main", run_name="__main__")


if __name__ == "__main__":
    main()
