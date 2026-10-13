"""Command-line entry point for DrainTracer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if __package__ in (None, ""):
    # Keep ``python src/main.py`` working when launched from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import (
    FAIL_ON_CHOICES,
    MAX_SAMPLE_INTERVAL,
    MAX_WARMUP,
    MAX_WINDOW,
    MAX_WINDOWS,
    MIN_SAMPLE_INTERVAL,
    ConfigError,
    MonitorSettings,
    resolve_settings,
)
from src.dashboard import DashboardApp
from src.diff_command import run_diff
from src.events import LeakSeverity
from src.file_picker import select_target_file
from src.headless import HeadlessRunner
from src.process_runner import ProcessManagerError
from src.report import DEFAULT_REPORT_DIR
from src.theme import PALETTE, blend
from src.units import parse_duration


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


def _duration_arg(value: str) -> float:
    try:
        return parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _time_limit(value: str) -> float:
    seconds = _duration_arg(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("the time limit must be greater than zero")
    return seconds


def _sample_interval(value: str) -> float:
    seconds = _duration_arg(value)
    if not MIN_SAMPLE_INTERVAL <= seconds <= MAX_SAMPLE_INTERVAL:
        raise argparse.ArgumentTypeError("the sample interval must be between 100ms and 1m")
    return seconds


def _warmup(value: str) -> float:
    seconds = _duration_arg(value)
    if seconds > MAX_WARMUP:
        raise argparse.ArgumentTypeError("the warm-up must be at most 24h")
    return seconds


def _window(value: str) -> float:
    seconds = _duration_arg(value)
    if not 0 < seconds <= MAX_WINDOW:
        raise argparse.ArgumentTypeError("windows must be longer than 0s and at most 1h")
    return seconds


_EXAMPLES = """\
examples:
  python src/main.py                                   pick a target in a file dialog
  python src/main.py --target app.exe -- --port 8080   launch with arguments
  python src/main.py --pid 4820                        attach to a running process
  python src/main.py --profile ci --target app.exe     use a .drainrc profile
  python src/main.py --warmup 15s --target server.exe  ignore startup growth
  python src/main.py --headless --target app.exe --timeout 2m --fail-on medium
  python src/main.py diff before.json after.json       compare two reports
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Windows process resources and detect likely leaks.",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_argument_group("target")
    source = target.add_mutually_exclusive_group()
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

    monitoring = parser.add_argument_group("monitoring")
    monitoring.add_argument(
        "--interval",
        type=_sample_interval,
        metavar="DURATION",
        help="Time between samples, e.g. 250ms or 2s (default: 500ms).",
    )
    monitoring.add_argument(
        "--window",
        type=_window,
        action="append",
        metavar="DURATION",
        help=(
            "Leak detection window, e.g. 30s or 2m. Repeat for several windows; "
            "replaces the configured ones (default: 10s, 30s, and 60s)."
        ),
    )
    monitoring.add_argument(
        "--warmup",
        type=_warmup,
        metavar="DURATION",
        help=(
            "Sample for DURATION before leak detection starts, so startup growth "
            "(cache warming, JIT, loading) is not reported, e.g. 10s (default: none)."
        ),
    )
    monitoring.add_argument(
        "--timeout",
        type=_time_limit,
        metavar="DURATION",
        help=(
            "Stop monitoring after DURATION, e.g. 90, 90s, 5m, or 1h30m. "
            "Without it, headless runs last until the process tree exits."
        ),
    )
    monitoring.add_argument(
        "--report-dir",
        metavar="DIR",
        help=(
            "Directory for exported reports, named leak_report_<UUID>.json and "
            f".html (default: {DEFAULT_REPORT_DIR})."
        ),
    )

    files = parser.add_argument_group("settings files")
    which = files.add_mutually_exclusive_group()
    which.add_argument(
        "--config",
        metavar="FILE",
        help=(
            "Read settings from FILE instead of the nearest .drainrc, "
            ".drainrc.toml, draintracer.toml, or config.toml with a "
            "[draintracer] table."
        ),
    )
    which.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore settings files; use built-in defaults and flags only.",
    )
    files.add_argument(
        "--profile",
        metavar="NAME",
        help="Apply the [profiles.NAME] section of the settings file.",
    )

    headless = parser.add_argument_group("headless")
    headless.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Run without the dashboard (for CI): print progress lines, export a "
            "report when monitoring ends, and exit 1 if a leak signal reaches "
            "--fail-on. Needs --target or --pid."
        ),
    )
    headless.add_argument(
        "--fail-on",
        choices=FAIL_ON_CHOICES,
        metavar="SEVERITY",
        help=(
            "The lowest leak severity that fails a headless run: low, medium, "
            "high (default), or critical."
        ),
    )
    parser.add_argument(
        "target_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the target after '--'.",
    )
    return parser


def _resolve_settings(args: argparse.Namespace) -> MonitorSettings:
    """Layer flag values over the settings file and its profile."""
    overrides: dict[str, Any] = {}
    if args.interval is not None:
        overrides["sample_interval_ms"] = round(args.interval * 1000)
    if args.window:
        overrides["windows"] = tuple(sorted(set(args.window)))
    if args.warmup is not None:
        overrides["warmup"] = args.warmup
    if args.timeout is not None:
        overrides["timeout"] = args.timeout
    if args.fail_on is not None:
        overrides["fail_on"] = LeakSeverity(args.fail_on)
    if args.report_dir is not None:
        overrides["report_dir"] = args.report_dir
    return resolve_settings(
        config_path=args.config,
        profile=args.profile,
        discover=not args.no_config,
        overrides=overrides,
    )


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
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["diff"]:
        return run_diff(arguments[1:], console)
    parser = build_parser()
    args = parser.parse_args(arguments)

    forwarded_args = list(args.target_args or [])
    if forwarded_args[:1] == ["--"]:
        forwarded_args = forwarded_args[1:]
    if args.pid is not None and forwarded_args:
        parser.error("arguments after '--' only apply to launched targets, not --pid")
    if args.fail_on is not None and not args.headless:
        parser.error("--fail-on only applies with --headless")
    if args.headless and args.target is None and args.pid is None:
        parser.error("--headless needs --target or --pid; the file picker is interactive")
    if args.profile is not None and args.no_config:
        parser.error("--profile needs a settings file; drop --no-config")
    if args.window and len(set(args.window)) > MAX_WINDOWS:
        parser.error(f"use at most {MAX_WINDOWS} different --window values")

    try:
        settings = _resolve_settings(args)
    except ConfigError as exc:
        console.print(Text.assemble(("Settings error: ", "bold red"), str(exc)))
        return 2

    if args.headless:
        return _run_headless(args, forwarded_args, console, settings)

    console.clear()
    _display_title(console)
    if settings.source is not None:
        console.print(Text(f"Using {settings.describe()}", style=PALETTE.muted))
    for warning in settings.warnings():
        console.print(Text.assemble(("Note: ", f"bold {PALETTE.warn}"), warning))

    if args.pid is not None:
        app = DashboardApp(
            None,
            console=console,
            report_dir=settings.report_dir,
            attach_pid=args.pid,
            timeout=settings.timeout,
            settings=settings,
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
        report_dir=settings.report_dir,
        timeout=settings.timeout,
        settings=settings,
    )
    return _run_dashboard(app, console)


def _run_headless(
    args: argparse.Namespace,
    forwarded_args: list[str],
    console: Console,
    settings: MonitorSettings,
) -> int:
    runner = HeadlessRunner(
        args.target,
        args=forwarded_args or None,
        console=console,
        report_dir=settings.report_dir,
        attach_pid=args.pid,
        timeout=settings.timeout,
        fail_on=settings.fail_on,
        settings=settings,
    )
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
