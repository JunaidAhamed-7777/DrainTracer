"""Keyboard-driven startup choices for launching or attaching to a target."""

from __future__ import annotations

from typing import Literal

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .theme import PALETTE


def choose_startup_action(console: Console) -> Literal["open", "hook"] | None:
    """Ask how to select a target, returning ``None`` on quit or end of input."""
    console.print(
        Panel(
            f"[bold {PALETTE.accent}]1[/]  [bold]Open a program[/]\n"
            f"[{PALETTE.muted}]   Start an executable or script.[/]\n\n"
            f"[bold {PALETTE.accent}]2[/]  [bold]Hook to a running program[/]\n"
            f"[{PALETTE.muted}]   Monitor an existing process by its PID.[/]",
            title=f"[bold {PALETTE.accent}] GET STARTED [/]",
            title_align="left",
            border_style=PALETTE.border,
            box=box.ROUNDED,
            padding=(1, 2),
            expand=False,
        )
    )
    while True:
        try:
            choice = console.input(Text("Choose 1 or 2 (Q to quit): ", style="bold"))
        except EOFError:
            return None
        choice = choice.strip().lower()
        if choice == "q":
            return None
        if choice in ("1", "2"):
            return "open" if choice == "1" else "hook"
        console.print(Text("Enter 1, 2, or Q.", style=PALETTE.warn))


def prompt_process_id(console: Console) -> int | None:
    """Read an attach PID; the process manager validates the live target."""
    console.print(Text("Find the PID in Task Manager > Details.", style=PALETTE.muted))
    console.print(
        Text("Quitting detaches and leaves the program running.", style=PALETTE.muted)
    )
    while True:
        try:
            value = console.input(Text("Process ID (Q to cancel): ", style="bold")).strip()
        except EOFError:
            return None
        if value.lower() == "q":
            return None
        try:
            pid = int(value)
        except ValueError:
            pass
        else:
            if pid > 0:
                return pid
        console.print(Text("Enter a positive process ID, or Q to cancel.", style=PALETTE.warn))
