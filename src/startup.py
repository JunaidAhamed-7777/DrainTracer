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
            f"[{PALETTE.muted}]   Browse and search running programs.[/]",
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
