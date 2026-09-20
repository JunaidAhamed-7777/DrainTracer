"""Command-line entry point for DrainTracer."""

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
        title = "DRAINTRACER"
    return title


def _display_title(console: Console) -> None:
    title = _load_title_art()
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        title.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        # Legacy Windows code pages cannot represent the block-art glyphs.
        # Keep startup reliable and show the same product title in plain text.
        title = "DRAINTRACER"
    console.print(title, style="bold cyan")


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
    _display_title(console)

    target_path = args.target
    if target_path is None:
        console.print(
            Panel(
                "[bold cyan]Select a target executable or script[/bold cyan]\n"
                "[dim]Supported: .exe | .bat | .cmd | .py[/dim]",
                title="[bold magenta]DRAINTRACER[/bold magenta]",
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
