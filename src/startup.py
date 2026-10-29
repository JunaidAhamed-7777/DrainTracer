"""Keyboard-driven startup choices for launching or attaching to a target."""

from __future__ import annotations

from typing import Literal

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .picker_input import PickerKeyboard, supports_picker_keyboard
from .theme import PALETTE

_ACTIONS = (
    ("open", "Open a program", "Start an executable or script."),
    ("hook", "Hook to a running program", "Browse and search running programs."),
)


def _startup_view(selected: int | None = None) -> Group:
    """Keep the navigation hint below and outside the choices panel."""
    choices = Text()
    for index, (_action, label, description) in enumerate(_ACTIONS):
        if index:
            choices.append("\n\n")
        marker = str(index + 1) if selected is None else ">" if index == selected else " "
        style = f"bold {PALETTE.accent} on {PALETTE.surface}" if index == selected else "bold"
        choices.append(f"{marker}  {label}", style=style)
        choices.append(f"\n   {description}", style=PALETTE.muted)
    hint = "Up/Down: move | Space/Enter: select | Q/Esc: quit"
    if selected is None:
        hint = "Enter 1 or 2 to select | Q to quit"
    return Group(
        Panel(
            choices,
            title=f"[bold {PALETTE.accent}] GET STARTED [/]",
            title_align="left",
            border_style=PALETTE.border,
            box=box.ROUNDED,
            padding=(1, 2),
            expand=False,
        ),
        Text(hint, style=PALETTE.muted),
    )


def choose_startup_action(console: Console) -> Literal["open", "hook"] | None:
    """Ask how to select a target, returning ``None`` on quit or end of input."""
    if supports_picker_keyboard(console):
        keyboard = PickerKeyboard()
        selected = 0
        with keyboard, Live(_startup_view(selected), console=console, auto_refresh=False) as live:
            while True:
                try:
                    key = keyboard.read()
                except EOFError:
                    return None
                if key.kind == "escape" or (key.kind == "text" and key.text.lower() == "q"):
                    return None
                if key.kind == "enter" or (key.kind == "text" and key.text == " "):
                    return "open" if selected == 0 else "hook"
                if key.kind in ("up", "down"):
                    selected = min(max(0, selected + (1 if key.kind == "down" else -1)), 1)
                    live.update(_startup_view(selected), refresh=True)

    console.print(_startup_view())
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
