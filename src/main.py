"""Command-line entry point for Process Resource Leak Detector."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

if __package__ in (None, ""):
    # Keep ``python src/main.py`` working when launched from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import DashboardApp
from src.file_picker import select_target_file
from src.process_runner import ProcessManagerError


def _load_title_art() -> str:
    title_path = Path(__file__).resolve().parents[1] / "TITLE.txt"
    try:
        title = title_path.read_text(encoding="utf-8").strip()
    except OSError:
        title = "PROCESS RESOURCE LEAK DETECTOR"
    return title


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Windows process resources and detect likely leaks.",
    )
    parser.add_argument(
        "--target",
        metavar="PATH",
        help="Target executable/script. If omitted, open the native file picker.",
    )
    parser.add_argument(
        "--export",
        default="leak_report.json",
        metavar="PATH",
        help="Report path used by the [E] export action.",
    )
    parser.add_argument(
        "target_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the target after '--'.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    console = Console()
    args = build_parser().parse_args(argv)
    console.clear()
    console.print(_load_title_art(), style="bold cyan")

    target_path = args.target
    if target_path is None:
        console.print(
            Panel(
                "[bold cyan]Select a target executable or script[/bold cyan]\n"
                "[dim]Supported: .exe | .bat | .cmd | .py[/dim]",
                title="[bold magenta]PROCESS RESOURCE LEAK DETECTOR[/bold magenta]",
                border_style="magenta",
            )
        )
        try:
            target_path = select_target_file()
        except FileNotFoundError as exc:
            console.print(f"[bold red]File picker unavailable:[/bold red] {exc}")
            return 1
        except OSError as exc:
            console.print(f"[bold red]File picker failed:[/bold red] {exc}")
            return 1

    if not target_path:
        console.print("[yellow]Selection canceled. Nothing to monitor.[/yellow]")
        return 0

    app = DashboardApp(
        target_path,
        args=args.target_args or None,
        console=console,
        export_path=args.export,
    )
    try:
        return app.run()
    except ProcessManagerError as exc:
        console.print(f"[bold red]Process error:[/bold red] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
