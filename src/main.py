"""Command-line entry point for DrainTracer."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_INSTALLATION_MANIFEST = "runtime-manifest.json"
_INSTALLATION_CORE = frozenset({
    _INSTALLATION_MANIFEST,
    "src/main.py",
    "src/__init__.py",
    "requirements.txt",
    "TITLE.txt",
    "leak_reports/frames/report.css",
    "leak_reports/frames/report.js",
})


def _installation_context() -> tuple[Path, bool]:
    """Distinguish the preserved source layout from the wheel's package layout."""
    package = Path(__file__).resolve().parent
    packaged = package.name != "src"
    return (package if packaged else package.parent), packaged


def _installation_status(
    root: Path, *, packaged: bool = False,
) -> tuple[tuple[str, ...], str | None]:
    """Read the committed snapshot without importing application dependencies.

    The snapshot is never regenerated from the running installation: doing so
    could silently accept an incomplete checkout as the expected structure.
    """
    root = root.resolve()

    def installed_path(relative: str) -> Path:
        if not packaged:
            return root / relative
        if relative.startswith("src/"):
            return root / relative[4:]
        return root / "_data" / relative

    manifest = installed_path(_INSTALLATION_MANIFEST)
    try:
        if not manifest.resolve().is_relative_to(root):
            return (), "The runtime snapshot resolves outside the installation."
        if not manifest.is_file():
            return (_INSTALLATION_MANIFEST,), None
        snapshot = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return (), f"Cannot read the runtime snapshot: {exc}"
    if (not isinstance(snapshot, dict)
            or type(snapshot.get("schema")) is not int or snapshot["schema"] != 1):
        return (), "The runtime snapshot has an unsupported schema."
    required = snapshot.get("required_files")
    if not isinstance(required, list) or not required:
        return (), "The runtime snapshot needs a nonempty required_files list."
    for relative in required:
        if (not isinstance(relative, str) or not relative
                or any(character in relative for character in "\\:\x00")
                or any(ord(character) < 32 for character in relative)
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            return (), "The runtime snapshot contains an unsafe file path."
    if len(set(required)) != len(required) or not _INSTALLATION_CORE.issubset(required):
        return (), "The runtime snapshot is duplicated or omits core files."
    missing = []
    try:
        for relative in required:
            path = installed_path(relative)
            if not path.resolve().is_relative_to(root):
                return (), f"Required file resolves outside the installation: {relative}"
            if not path.is_file():
                missing.append(relative)
    except (OSError, ValueError) as exc:
        return (), f"Cannot inspect required installation files: {exc}"
    return tuple(sorted(missing)), None


def _run_installation_git(root: Path, args: list[str]) -> str:
    """Run bounded, noninteractive Git without a shell or application imports."""
    environment = os.environ.copy()
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=environment,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or is not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git timed out after 60 seconds; check network access.") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot run Git: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _repair_installation(root: Path) -> bool:
    """Pull normally, then restore only absent required files tracked by Git."""
    root = root.resolve()
    try:
        checkout = Path(_run_installation_git(root, ["rev-parse", "--show-toplevel"]))
        if checkout.resolve() != root:
            raise RuntimeError("The installation is not the root of its own Git checkout.")
        print("Running git pull --ff-only origin main...")
        output = _run_installation_git(root, ["pull", "--ff-only", "origin", "main"])
        if output:
            print(output)
        # If the snapshot was missing, restore it first and then read its
        # inventory. Never infer the expected tree from the incomplete folder.
        for attempt in range(3):
            missing, error = _installation_status(root)
            if error:
                raise RuntimeError(error)
            if not missing:
                return True
            if attempt == 2:
                raise RuntimeError("Required files are still missing after recovery.")
            for relative in missing:
                path = root / relative
                if not path.resolve().is_relative_to(root):
                    raise RuntimeError(f"Refusing to restore outside the installation: {relative}")
                if path.exists() or path.is_symlink():
                    raise RuntimeError(f"An existing path blocks the required file: {relative}")
                # An index deletion is a staged user change, not a repair target.
                _run_installation_git(root, ["ls-files", "--error-unmatch", "--", relative])
            _run_installation_git(
                root, ["restore", "--source=HEAD", "--worktree", "--", *missing]
            )
        return False
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Recovery failed: {exc}")
        print("Restore the listed files in this checkout and run DrainTracer again.")
        return False


def _installation_input_is_terminal() -> bool:
    """Exclude pipes and Windows NUL, which misleadingly reports isatty()."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetConsoleMode(
                ctypes.c_void_p(handle), ctypes.byref(mode)
            ))
        return True
    except (OSError, TypeError, ValueError):
        return False


def _restart_installation(root: Path, argv: list[str]) -> int:
    """Keep the same console and wait for the restarted CLI, including on Windows."""
    environment = os.environ.copy()
    environment["_DRAINTRACER_REPAIR_ATTEMPTED"] = "1"
    with subprocess.Popen(
        [sys.executable, str(root / "src/main.py"), *argv], env=environment
    ) as restarted:
        while True:
            try:
                return restarted.wait()
            except KeyboardInterrupt:
                # The child receives the same console event. Let its normal
                # cleanup finish instead of killing it while it owns a target.
                continue


def _check_installation(
    root: Path, argv: list[str], *, packaged: bool = False,
) -> int | None:
    """Return None to continue startup, or an exit code after recovery/cancel."""
    attempted = os.environ.pop("_DRAINTRACER_REPAIR_ATTEMPTED", None) == "1"
    if packaged:
        missing, error = _installation_status(root, packaged=True)
    else:
        missing, error = _installation_status(root)
    if not missing and error is None:
        return None
    print("DrainTracer installation check failed.")
    print(f"Installation folder: {root}")
    if error:
        print(error)
    if packaged:
        if missing:
            print("Missing required files:")
            for relative in missing:
                print(f"  - {relative}")
        print("Reinstall DrainTracer from the wheel, source archive, or package source you used:")
        prefix = "& " if os.name == "nt" else ""
        print(f'  {prefix}"{sys.executable}" -m pip install --force-reinstall <package-source>')
        print("No files were changed. Installed packages are repaired with pip.")
        return 2
    if error:
        print("Review runtime-manifest.json or restore it from Git, then start again.")
        return 2
    print("Missing required files:")
    for relative in missing:
        print(f"  - {relative}")
    if attempted:
        print("Recovery was already attempted. Restore the missing files before starting again.")
        return 2
    arguments = argv[:argv.index("--")] if "--" in argv else argv
    if "--headless" in arguments or not _installation_input_is_terminal():
        print("Automatic recovery needs an interactive terminal; no files were changed.")
        print("Run DrainTracer interactively, or pull and restore the missing files manually.")
        return 2
    while True:
        try:
            response = input(
                "Press Enter to git pull and restore missing required files (Q to quit): "
            ).strip().lower()
        except EOFError:
            print("Recovery canceled.")
            return 2
        except KeyboardInterrupt:
            print("\nRecovery canceled.")
            return 130
        if response == "q":
            print("Recovery canceled.")
            return 2
        if not response:
            break
        print("Press Enter to recover, or Q to quit.")
    try:
        if not _repair_installation(root):
            return 2
        print("Required files are available. Restarting DrainTracer...")
        sys.stdout.flush()
        sys.stderr.flush()
        return _restart_installation(root, argv)
    except KeyboardInterrupt:
        print("\nRecovery interrupted.")
        return 130
    except OSError as exc:
        print(f"Cannot restart DrainTracer: {exc}")
        return 2


def _make_output_safe() -> None:
    """Replace characters the output encoding cannot show instead of crashing.

    Piped output on Windows (CI logs, redirection) uses the ANSI code page,
    which cannot encode every process name or path.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


# This entry-point-only check must precede Rich and every application import.
# Keeping it here also lets recovery run when an entire backend module is gone.
if __name__ == "__main__":
    _make_output_safe()
    _runtime_root, _packaged = _installation_context()
    _startup_result = _check_installation(_runtime_root, sys.argv[1:], packaged=_packaged)
    if _startup_result is not None:
        raise SystemExit(_startup_result)

if __package__ in (None, ""):
    # Keep ``python src/main.py`` working when launched from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "src"

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    from .budgets import BUDGET_OPTIONS, parse_budget
    from .config import (
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
    from .dashboard import DashboardApp
    from .diff_command import run_diff
    from .events import METRIC_LABELS, LeakSeverity
    from .file_picker import select_target_file
    from .headless import EXIT_ERROR, HeadlessRunner
    from .process_picker import select_running_process
    from .process_runner import ProcessManagerError
    from .report import DEFAULT_REPORT_DIR
    from .resources import resource_path
    from .startup import choose_startup_action
    from .theme import PALETTE, blend, plain_console
    from .units import parse_count, parse_duration
    from .updates import check_for_update
except ModuleNotFoundError as _dependency_error:
    if (__name__ != "__main__"
            or (_dependency_error.name or "").split(".")[0] not in {"rich", "psutil", "tomli"}):
        raise
    print(f"DrainTracer needs the Python dependency: {_dependency_error.name}")
    print("Install the requirements with the same Python, then start again:")
    _runtime_root, _packaged = _installation_context()
    _requirements = _runtime_root / ("_data/requirements.txt" if _packaged else "requirements.txt")
    _prefix = "& " if os.name == "nt" else ""
    print(f'  {_prefix}"{sys.executable}" -m pip install -r "{_requirements}"')
    raise SystemExit(2) from None


def _load_title_art() -> str:
    title_path = resource_path("TITLE.txt")
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
        f"[{PALETTE.muted}]Resource leak monitor for Windows and Linux process trees[/] "
        f"[bold {PALETTE.accent}]{DashboardApp.VERSION}[/]"
    )
    status = check_for_update(DashboardApp.VERSION) if console.is_terminal else None
    console.print(_update_notice(status))
    console.print()


def _update_notice(status: bool | None) -> Text:
    """Show only confirmed updates; keep unavailable checks distinct."""
    if status is True:
        return Text.assemble(
            ("New Version Available!", "red"),
            " Update using ",
            ("pip install --upgrade draintracer", "green"),
        )
    prefix = "No new updates." if status is False else "Update check unavailable."
    return Text.assemble(
        f"{prefix} Contribute at ",
        ("github.com/JunaidAhamed-7777/DrainTracer",
         "blue link https://github.com/JunaidAhamed-7777/DrainTracer"),
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


def _operations(value: str) -> float:
    try:
        count = parse_count(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not count > 0:
        raise argparse.ArgumentTypeError("the operation count must be greater than zero")
    return count


def _budget(metric: str):
    def parse(value: str) -> float:
        try:
            return parse_budget(metric, value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


def _window(value: str) -> float:
    seconds = _duration_arg(value)
    if not 0 < seconds <= MAX_WINDOW:
        raise argparse.ArgumentTypeError("windows must be longer than 0s and at most 1h")
    return seconds


_EXAMPLES = """\
examples:
  draintracer                                      choose to open or hook to a program
  draintracer --target app.exe -- --port 8080        launch with arguments
  draintracer --pid 4820                            attach to a running process
  draintracer --profile ci --target app.exe          use a .drainrc profile
  draintracer --warmup 15s --target server.exe        ignore startup growth
  draintracer --headless --target app.exe --timeout 2m --fail-on medium
  draintracer --headless --target app.exe --workload-id import-1k --operations 1000
  draintracer --headless --target app.exe --budget-private 500MB --budget-handles 2000
  draintracer diff before.json after.json            compare two reports
  draintracer doctor --target worker.py --json       check setup without starting a target
  draintracer init                                  preview a validated project config
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="draintracer",
        description="Monitor Windows and Linux process resources and detect likely leaks.",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"DrainTracer {DashboardApp.VERSION}")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors, including forced color.")
    parser.add_argument("--plain", action="store_true", help="Use static, unstyled headless text output.")
    parser.add_argument("--quiet", action="store_true", help="Use headless output with warnings, signals, errors, and the final summary only.")
    target = parser.add_argument_group("target")
    source = target.add_mutually_exclusive_group()
    source.add_argument(
        "--target",
        metavar="PATH",
        help="Target executable/script. Without --target or --pid, choose how to start.",
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
            "Report root; exports are grouped by target filename and date "
            f"(default: {DEFAULT_REPORT_DIR})."
        ),
    )

    experiment = parser.add_argument_group(
        "experiment",
        "Labels recorded in the report so comparisons can tell equivalent runs apart. "
        "Target arguments are only counted and fingerprinted, never stored.",
    )
    experiment.add_argument(
        "--workload-id",
        metavar="ID",
        help="Name of the workload this run exercised, e.g. import-1k.",
    )
    experiment.add_argument(
        "--build",
        metavar="LABEL",
        help="Application build or version under test, e.g. 1.4.2 or a commit hash.",
    )
    experiment.add_argument(
        "--operations",
        type=_operations,
        metavar="N",
        help=(
            "Operations or input size the workload processed; comparisons show "
            "growth per operation when both runs set it."
        ),
    )
    experiment.add_argument(
        "--note",
        metavar="TEXT",
        help="Free-text note; secret-looking values such as password=... are redacted.",
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
    headless.add_argument(
        "--fail-on-inconclusive",
        action="store_true",
        default=None,
        help=(
            "Exit 3 instead of 0 when a headless run ends without judging a leak "
            "window (too short, warm-up unfinished, or nothing readable)."
        ),
    )
    headless.add_argument(
        "--fail-on-target-error",
        action="store_true",
        default=None,
        help="Exit 4 when a launched target exits by itself with a non-zero code.",
    )
    for metric, option in BUDGET_OPTIONS.items():
        what = "a size such as 500MB" if metric == "private_bytes" else "a positive whole count"
        headless.add_argument(
            f"--{option.replace('_', '-')}",
            type=_budget(metric),
            metavar="SIZE" if metric == "private_bytes" else "N",
            dest=option,
            help=(
                f"Exit 5 when peak {METRIC_LABELS[metric]} exceed {what}, even "
                "without a leak signal."
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
    for key in ("fail_on_inconclusive", "fail_on_target_error"):
        if getattr(args, key) is not None:
            overrides[key] = getattr(args, key)
    if args.report_dir is not None:
        overrides["report_dir"] = args.report_dir
    for key in ("workload_id", "build", "operations", "note"):
        if getattr(args, key) is not None:
            overrides[key] = getattr(args, key)
    budgets = {
        metric: getattr(args, option)
        for metric, option in BUDGET_OPTIONS.items()
        if getattr(args, option) is not None
    }
    if budgets:
        overrides["budgets"] = budgets
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
        console.print(Text.assemble(("Process error: ", "bold red"), str(exc)))
        return 1
    except Exception:
        return _report_crash(console, 1)


def _prompt_target_path(console: Console) -> str | None:
    """Keep file selection usable on Linux without Tk or a desktop display."""
    console.print(Text("Enter an executable or script path. Blank input or Q cancels.", style=PALETTE.muted))
    try:
        value = input("Target path: ").strip()
    except EOFError:
        return None
    if not value or value.lower() == "q":
        return None
    return str(Path(value).expanduser())


def _report_crash(console: Console, code: int) -> int:
    """Show an unexpected error; the session has already cleaned up its target."""
    console.print_exception(show_locals=False)
    console.print(
        Text.assemble(
            ("DrainTracer stopped after an unexpected error. ", "bold red"),
            "Its target was cleaned up as on a normal exit. Please report the traceback above.",
        )
    )
    return code


def _configure_logging() -> logging.Handler | None:
    """Keep diagnostics off the terminal; ``DRAINTRACER_LOG`` sends them to a file.

    Without a handler, Python prints warnings and tracebacks from the
    sampling threads straight to stderr, which tears through the live
    dashboard and interleaves with headless output. Sampling and display
    failures still reach the event log. Logging that is already configured
    (DrainTracer embedded in another program) is left alone.
    """
    root = logging.getLogger()
    if root.handlers:
        return None
    handler: logging.Handler = logging.NullHandler()
    path = os.environ.get("DRAINTRACER_LOG")
    if path:
        try:
            handler = logging.FileHandler(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"DRAINTRACER_LOG: cannot write {path}: {exc.strerror or exc}", file=sys.stderr)
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
            )
            root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return handler


def main(argv: list[str] | None = None) -> int:
    _make_output_safe()
    if __name__ != "__main__":
        arguments = list(sys.argv[1:] if argv is None else argv)
        root, packaged = _installation_context()
        startup_result = _check_installation(root, arguments, packaged=packaged)
        if startup_result is not None:
            return startup_result
    _configure_logging()
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
    if arguments[:1] == ["doctor"]:
        from .preflight import run_doctor

        return run_doctor(arguments[1:], console, parser_factory=build_parser,
                          settings_resolver=_resolve_settings)
    if arguments[:1] == ["init"]:
        from .preflight import run_init

        return run_init(arguments[1:], console)
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.no_color:
        console.no_color = True
    if args.plain:
        console = plain_console(console)
    args.headless = args.headless or args.plain or args.quiet

    forwarded_args = list(args.target_args or [])
    if forwarded_args[:1] == ["--"]:
        forwarded_args = forwarded_args[1:]
    if args.pid is not None and forwarded_args:
        parser.error("arguments after '--' only apply to launched targets, not --pid")
    if args.fail_on is not None and not args.headless:
        parser.error("--fail-on only applies with --headless")
    for flag, value in (
        ("--fail-on-inconclusive", args.fail_on_inconclusive),
        ("--fail-on-target-error", args.fail_on_target_error),
    ):
        if value and not args.headless:
            parser.error(f"{flag} only applies with --headless")
    for option in BUDGET_OPTIONS.values():
        if getattr(args, option) is not None and not args.headless:
            parser.error(f"--{option.replace('_', '-')} only applies with --headless")
    if args.headless and args.target is None and args.pid is None:
        parser.error("--headless needs --target or --pid; startup selection is interactive")
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
    if not console.is_terminal:
        console.print(
            Text(
                "Output is not a terminal, so the live dashboard will only show its final "
                "frame. Use --headless for logs and CI.",
                style=PALETTE.warn,
            )
        )
    if settings.source is not None:
        console.print(Text(f"Using {settings.describe()}", style=PALETTE.muted))
    for warning in settings.warnings():
        console.print(Text.assemble(("Note: ", f"bold {PALETTE.warn}"), warning))

    target_path = args.target
    attach_pid = args.pid
    if target_path is None and attach_pid is None:
        action = choose_startup_action(console)
        if action is None:
            console.print("[yellow]Selection canceled. Nothing to monitor.[/yellow]")
            return 0
        if action == "hook":
            if forwarded_args:
                parser.error(
                    "arguments after '--' only apply to launched targets, "
                    "not Hook to a running program"
                )
            attach_pid = select_running_process(console)
            if attach_pid is None:
                console.print("[yellow]Selection canceled. Nothing to monitor.[/yellow]")
                return 0

    if attach_pid is not None:
        app = DashboardApp(
            None,
            console=console,
            report_dir=settings.report_dir,
            attach_pid=attach_pid,
            timeout=settings.timeout,
            settings=settings,
        )
        return _run_dashboard(app, console)

    if target_path is None:
        supported = (
            ".exe  .bat  .cmd  .py" if sys.platform == "win32" else
            "Linux executables, executable #! scripts, and .py files"
        )
        console.print(
            Panel(
                "[bold]Select a target executable or script[/bold]\n"
                f"[{PALETTE.muted}]Supported: {supported}[/]",
                title=f"[bold {PALETTE.accent}] CHOOSE TARGET [/]",
                title_align="left",
                border_style=PALETTE.border,
                box=box.ROUNDED,
                padding=(1, 2),
                expand=False,
            )
        )
        hint = ("\nPass the program with --target PATH instead.", "dim")
        try:
            target_path = select_target_file()
        except OSError as exc:
            reason = f"{exc}: {exc.__cause__}" if exc.__cause__ else str(exc)
            console.print(Text.assemble(("File picker failed: ", "bold red"), reason, hint))
            if not sys.platform.startswith("linux"):
                return 1
            target_path = _prompt_target_path(console)

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
        fail_on_inconclusive=settings.fail_on_inconclusive,
        fail_on_target_error=settings.fail_on_target_error,
        quiet=args.quiet,
    )
    try:
        return runner.run()
    except Exception:
        return _report_crash(console, EXIT_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
