"""Command-line entry point for DrainTracer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if __package__ in (None, ""):
    # Keep ``python src/main.py`` working when launched from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import DashboardApp
from src.file_picker import select_target_file
from src.process_runner import ProcessManagerError
from src.report import DEFAULT_REPORT_DIR
from src.theme import PALETTE, blend


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
    lines = title.splitlines() or ["DRAINTRACER"]
    if max(len(line) for line in lines) > console.width:
        # Wrapped block art is unreadable; narrow terminals get plain text.
        lines = ["DRAINTRACER"]

    art = Text()
    for index, line in enumerate(lines):
        shade = blend(PALETTE.cyan, PALETTE.accent_alt, index / max(1, len(lines) - 1))
        art.append(line + "\n", style=f"bold {shade}")
    console.print(art, end="")
    console.print(
        f"[{PALETTE.muted}]Resource leak monitor for Windows process trees[/] "
        f"[bold {PALETTE.accent}]{DashboardApp.VERSION}[/]\n"
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a process ID, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("process IDs are positive integers")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Windows process resources and detect likely leaks.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--target",
        metavar="PATH",
        help="Target executable/script. If omitted, open the native file picker.",
    )
    source.add_argument(
        "--pid",
        type=_positive_int,
        metavar="PID",
        help=(
            "Attach to an already running process instead of launching one. "
            "Quitting detaches and leaves it running."
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        metavar="DIR",
        help=(
            "Directory for exported reports, named leak_report_<UUID>.json "
            f"(default: {DEFAULT_REPORT_DIR})."
        ),
    )
    parser.add_argument(
        "target_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the target after '--'.",
    )
    return parser


def _run_dashboard(app: DashboardApp, console: Console) -> int:
    try:
        return app.run()
    except ProcessManagerError as exc:
        console.print(f"[bold red]Process error:[/bold red] {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    console = Console()
    try:
        return _main(console, argv)
    except KeyboardInterrupt:
        # Startup cleans up after itself before re-raising; just exit quietly.
        console.print("[yellow]Interrupted.[/yellow]")
        return 130


def _main(console: Console, argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    forwarded_args = list(args.target_args or [])
    if forwarded_args[:1] == ["--"]:
        forwarded_args = forwarded_args[1:]
    if args.pid is not None and forwarded_args:
        parser.error("arguments after '--' only apply to launched targets, not --pid")

    console.clear()
    _display_title(console)

    if args.pid is not None:
        app = DashboardApp(
            None,
            console=console,
            report_dir=args.report_dir,
            attach_pid=args.pid,
        )
        return _run_dashboard(app, console)

    target_path = args.target
    if target_path is None:
        console.print(
            Panel(
                "[bold]Select a target executable or script[/bold]\n"
                f"[{PALETTE.muted}]Supported: .exe  .bat  .cmd  .py[/]",
                title=f"[bold {PALETTE.accent}] CHOOSE TARGET [/]",
                title_align="left",
                border_style=PALETTE.border,
                box=box.ROUNDED,
                padding=(1, 2),
                expand=False,
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
        args=forwarded_args or None,
        console=console,
        report_dir=args.report_dir,
    )
    return _run_dashboard(app, console)


if __name__ == "__main__":
    raise SystemExit(main())
