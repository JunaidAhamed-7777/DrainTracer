"""Live system overview and startup choices for launching or attaching."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from threading import Event, Thread
from typing import Literal

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .keys import KeyReader, stdin_is_interactive, stream_is_tty
from .system_overview import (
    DEFAULT_PROCESS_LIMIT,
    RankedProcess,
    SystemOverview,
    SystemOverviewSampler,
)
from .theme import PALETTE, select_glyphs
from .units import format_bytes, safe_label
from .widgets import LegacySafe

StartupAction = Literal["open", "hook"]
_CPU_WARMUP = 0.25
_SAMPLE_INTERVAL = 1.0
_INPUT_INTERVAL = 0.05

_ACTIONS = (
    "Open a program",
    "Hook to a running program",
)


def _actions(console: Console, selected: int | None, *, held: bool = False) -> RenderableType:
    choices = []
    labels = _ACTIONS if console.width >= 35 else ("Open a program", "Hook a process")
    for index, label in enumerate(labels):
        focused = index == selected
        key = f"{'>' if focused else ' '} [{index + 1}] "
        choice = Text.assemble((key, f"bold {PALETTE.accent}"), (label, "bold"))
        if focused:
            choice.stylize(f"bold {PALETTE.ink} on {PALETTE.accent}")
        choices.append(choice)
    if selected is None:
        return Group(*choices, Text("Enter 1 or 2 to select | Q to quit", style=PALETTE.muted))

    key_style = f"bold {PALETTE.teal}"
    move = Text.assemble(("Up/Down", key_style), " move   " if console.width >= 33 else "   ",
                         ("Enter", key_style), " select")
    session = Text.assemble(("P", key_style), " resume   " if held else " hold   ", ("Q/Esc", key_style), " quit")
    if console.width >= 68:
        menu = Table.grid(padding=(0, 3), pad_edge=False)
        menu.add_column(no_wrap=True)
        menu.add_column(style=PALETTE.muted)
        menu.add_row(choices[0], "Pick an executable or script")
        menu.add_row(choices[1], "Search by process name or PID")
        layout = Group(menu, Text.assemble(move, "     ", session))
    else:
        layout = Group(*choices, move, session)
    if console.height < 18:
        return layout
    return Panel(
        layout, title=Text(" CHOOSE A TARGET ", style=f"bold {PALETTE.accent}"), title_align="left",
        border_style=PALETTE.border, box=box.ROUNDED, padding=(0, 1),
    )


def _capacity(used: int | None, total: int | None, empty: str, *, compact: bool) -> tuple[str, float | None]:
    if used is None or total is None:
        return empty, None
    if total == 0:
        return "Off", None
    percent = 100 * used / total
    reading = f"{format_bytes(used)} / {format_bytes(total)}"
    if not compact:
        reading += f" ({percent:.1f}%)"
    return reading, percent


def _host_resources(console: Console, overview: SystemOverview | None, held: bool, rows: range) -> Panel:
    empty = "Sampling..." if overview is None else "Unavailable"
    cpu_percent = overview.cpu_percent if overview else None
    cpu = "Sampling..." if overview and overview.cpu_warming_up else empty
    if cpu_percent is not None:
        cpu = f"{cpu_percent:.1f}%"
    compact = console.width < 39 or 68 <= console.width < 76
    memory, ram_percent = _capacity(
        overview.used_memory_bytes if overview else None, overview.total_memory_bytes if overview else None,
        empty, compact=compact,
    )
    swap, swap_percent = _capacity(
        overview.used_swap_bytes if overview else None, overview.total_swap_bytes if overview else None,
        empty, compact=compact,
    )
    capacity = Table.grid(padding=(0, 1), pad_edge=False)
    capacity.add_column(no_wrap=True)
    meter_width = min(16, (console.width - 64) // 3) if console.width >= 76 else 0
    if meter_width:
        capacity.add_column()
    capacity.add_column(no_wrap=True)
    metrics = (
        ("CPU", cpu, cpu_percent, PALETTE.cyan), ("RAM", memory, ram_percent, PALETTE.accent_alt),
        ("Swap", swap, swap_percent, PALETTE.teal),
    )
    glyphs = select_glyphs(console)
    for index, (label, reading, percent, color) in enumerate(metrics):
        if index not in rows:
            continue
        cells = [Text(label, style=f"bold {color}")]
        if meter_width:
            filled = round(meter_width * min(100, max(0, percent or 0)) / 100)
            cells.append(Text.assemble(
                (glyphs.spark[-1] * filled, color), ("." * (meter_width - filled), PALETTE.border),
            ))
        cells.append(Text(reading))
        capacity.add_row(*cells)

    activity = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
    activity.add_column(ratio=1, no_wrap=True)
    activity.add_column(justify="right", no_wrap=True)
    index = 0 if console.width >= 68 else 3
    for labels, rates, color in (
        (("Disk read", "Disk write"), overview.disk if overview else None, PALETTE.accent),
        (("Net receive", "Net send"), overview.network if overview else None, PALETTE.teal),
    ):
        for label, rate in zip(labels, (rates.incoming, rates.outgoing) if rates else (None, None)):
            if index in rows:
                reading = "Sampling..." if rates is not None else empty
                if rate is not None:
                    reading = f"{format_bytes(rate)}/s"
                activity.add_row(Text(label, style=f"bold {color}"), Text(reading))
            index += 1

    if console.width >= 68:
        values = Table.grid(expand=True, padding=(0, 3), pad_edge=False)
        values.add_column(ratio=3)
        values.add_column(ratio=2)
        values.add_row(capacity, activity)
    else:
        values = Group(capacity, activity)
    state = "On hold" if held else "Live"
    if overview and overview.sampled_at is not None:
        state += " " + time.strftime("%H:%M:%S", time.localtime(overview.sampled_at))
    return Panel(
        values, title=f"[bold {PALETTE.accent}] SYSTEM OVERVIEW [/]", title_align="left",
        subtitle=state, subtitle_align="right",
        border_style=PALETTE.border, box=box.ROUNDED, padding=(0, 1),
    )


def _ranked_list(
    title: str,
    rows: tuple[RankedProcess, ...],
    format_value: Callable[[int | float], str],
    empty: str,
) -> Group:
    heading = Rule(Text(title, style=f"bold {PALETTE.cyan}"), align="left", style=PALETTE.border)
    if not rows:
        return Group(heading, Text(empty, style=PALETTE.muted, no_wrap=True, overflow="ellipsis"))

    table = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
    table.add_column(ratio=1, no_wrap=True)
    table.add_column(justify="right", no_wrap=True, style=PALETTE.muted)
    table.add_column(justify="right", no_wrap=True)
    for row in rows:
        name = safe_label(row.process.name)
        table.add_row(Text(name, overflow="ellipsis"), f"PID {row.process.pid}", format_value(row.value))
    return Group(heading, table)


def _startup_view(
    console: Console, overview: SystemOverview | None, selected: int, page: int, *, held: bool = False,
) -> tuple[RenderableType, int]:
    """Keep the chooser visible and page host readings and rankings to fit."""
    wide = console.width >= 68
    groups = (
        ("CPU (% of one core)", overview.top_cpu if overview else (), lambda value: f"{value:.1f}%"),
        ("Memory (RSS)", overview.top_memory if overview else (), format_bytes),
        (overview.handle_label if overview else "Handles / FDs",
         overview.top_handles if overview else (), lambda value: f"{value:,}"),
        ("Threads", overview.top_threads if overview else (), lambda value: f"{value:,}"),
    )
    columns = 2 if wide else 1
    bands = len(groups) // columns
    host_rows = 4 if wide else 7
    action_rows = 5 if wide else 6
    if console.height < 18:
        action_rows -= 2
    # Each band needs a heading and one process. Short terminals give host
    # readings their own page so the last ranking remains reachable.
    space = console.height - action_rows - 1
    separate = space < host_rows + 2 + 2 * bands
    rows_per_host_page = max(1, space - 2) if separate else host_rows
    host_pages = (host_rows + rows_per_host_page - 1) // rows_per_host_page if separate else 0
    rank_space = space if separate else space - host_rows - 2
    bands_shown = max(1, min(bands, rank_space // 2))
    per_page = max(1, rank_space // bands_shown - 1)
    group_size = columns * bands_shown
    rank_pages = []
    for index in range(0, len(groups), group_size):
        visible = groups[index:index + group_size]
        longest = max(1, *(len(rows) for _, rows, _ in visible))
        rank_pages.extend((visible, start) for start in range(0, longest, per_page))
    pages = host_pages + len(rank_pages)
    page = min(max(0, page), pages - 1)
    host_page = page < host_pages
    label = "TOP PROCESSES" if console.width >= 32 else "Ranks"
    title = Text("Overview" if host_page else label, style=f"bold {PALETTE.accent}", no_wrap=True)
    if pages > 1:
        title.append(f"  {page + 1}/{pages} Left/Right", style=PALETTE.muted)

    items: list[RenderableType] = [_actions(console, selected, held=held), title]
    if host_page or not separate:
        first = page * rows_per_host_page if host_page else 0
        host = _host_resources(console, overview, held, range(first, first + rows_per_host_page))
        if host_page:
            items.append(host)
        else:
            items.insert(0, host)
    if not host_page:
        visible, start = rank_pages[page - host_pages]
        lists = []
        for metric, rows, format_value in visible:
            empty = "Sampling..." if overview is None else "No more entries" if rows else "No readings"
            lists.append(_ranked_list(metric, rows[start:start + per_page], format_value, empty))
        if wide:
            grid = Table.grid(expand=True, padding=(0, 3), pad_edge=False)
            grid.add_column(ratio=1)
            grid.add_column(ratio=1)
            for index in range(0, len(lists), 2):
                grid.add_row(*lists[index:index + 2])
            items.append(grid)
        else:
            items.extend(lists)
    view = Group(*items)
    encoding = console.encoding or "utf-8"
    return (LegacySafe(view, encoding) if LegacySafe.needed(encoding) else view), page


def _collect_overviews(limit: int, updates: deque[SystemOverview], stop: Event) -> None:
    sampler = SystemOverviewSampler(limit)
    delay = _CPU_WARMUP
    while not stop.is_set():
        overview = sampler.sample(should_stop=stop.is_set)
        if stop.is_set():
            break
        updates.append(overview)
        stop.wait(delay)
        delay = _SAMPLE_INTERVAL


def _choose_live(console: Console, limit: int) -> StartupAction | None:
    updates: deque[SystemOverview] = deque(maxlen=1)
    stop = Event()
    worker = Thread(target=_collect_overviews, args=(limit, updates, stop),
                    name="draintracer-system-overview", daemon=True)
    worker.start()
    try:
        overview = None
        held = False
        selected = page = 0
        last_frame = None
        keyboard = KeyReader(native_windows=True)
        with keyboard as keys, Live(console=console, auto_refresh=False, transient=True) as live:
            while True:
                if updates and not held:
                    overview = updates.pop()
                size = console.size
                frame = (size, overview, selected, page, held)
                if frame != last_frame:
                    view, page = _startup_view(console, overview, selected, page, held=held)
                    live.update(view, refresh=True)
                    last_frame = (size, overview, selected, page, held)
                key = keys.read()
                if key in ("escape", "q", "Q", "\x04", "\x1a") or keys.eof:
                    return None
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("1", "2"):
                    return "open" if key == "1" else "hook"
                if key in ("enter", " "):
                    return "open" if selected == 0 else "hook"
                if key in ("up", "down"):
                    selected = int(key == "down")
                elif key in ("left", "right"):
                    page += 1 if key == "right" else -1
                elif key in ("p", "P") and overview is not None:
                    held = not held
                time.sleep(_INPUT_INTERVAL)
    finally:
        stop.set()
        worker.join(timeout=_INPUT_INTERVAL)


def choose_startup_action(
    console: Console, *, limit: int = DEFAULT_PROCESS_LIMIT,
) -> StartupAction | None:
    """Show the system overview, then select a target action or quit."""
    if console.is_terminal and stream_is_tty(console.file) and stdin_is_interactive():
        return _choose_live(console, limit)

    console.print(_actions(console, None))
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
