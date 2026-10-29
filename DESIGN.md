---
version: alpha
name: DrainTracer terminal UI
description: A compact instrument panel for inspecting Windows resource growth.
colors:
  primary: "#7aa2f7"
  accent: "#7aa2f7"
  muted: "#737aa2"
  border: "#3b4261"
  surface: "#24283b"
  danger: "#f7768e"
  ok: "#9ece6a"
typography:
  terminal:
    fontFamily: "monospace"
omitted:
  - section: rounded
    reason: Rich box glyphs define panel corners in terminal cells.
  - section: spacing
    reason: Rich layouts use character cells rather than CSS lengths.
  - section: components
    reason: Python render functions own terminal component configuration.
---

## Overview

DrainTracer serves developers monitoring Windows process trees. Preserve the
existing instrument-panel identity: startup artwork from TITLE.txt, blue
accents, quiet metadata, and dense resource views. Product familiarity and
readable measurements guide changes; avoid marketing layouts in the CLI.
The maintained product and workflow references are README.md and docs/usage.md.

## Colors

src/theme.py remains the canonical palette. Its PALETTE instance feeds
startup.py, process_picker.py, and dashboard.py directly; this document mirrors
the accepted values above; primary maps to PALETTE.accent. Selected rows
combine accent text with a surface
background and a literal `>` marker. Color always accompanies readable text.
The requested update notice uses Rich's named red, green, and blue colors for
the availability label, pip command, and repository link respectively.

## Typography

The user's terminal controls the monospace font and size. Bold labels identify
actions, muted lines explain them, and technical values remain literal. Keep
navigation hints in ASCII and preserve legacy Windows encoding fallbacks.

## Layout

Startup actions use one rounded Rich panel, with one blank row between actions
and padding of one vertical and two horizontal cells. Navigation help follows
outside the panel. Narrow terminals wrap prose and use the plain product title
when block artwork cannot fit. No browser layout or font dependency is needed.

## Elevation & Depth

Borders and selected-row backgrounds provide hierarchy; terminal panels use no
shadows or decorative overlays.

## Shapes

Reuse rich.box.ROUNDED for startup panels and Rich's legacy-console fallback.

## Components

src/picker_input.py owns native Windows keyboard input and terminal capability
checks. Startup and running-process choices reuse that reader. Startup uses
Up/Down and Spacebar/Enter, while the process picker keeps Enter so spaces remain
available in searches. Both expose selection with `>` and support cancellation.
Redirected input retains line prompts, and Ctrl+C reaches the entry point.
src/theme.py owns palette and glyph mapping; no generated token adapter exists.

The update status belongs directly below the version subtitle. A confirmed
newer release shows the upgrade command; a confirmed current release shows the
contribution link. Unavailable checks use a distinct status. Headless output,
help, and version-only commands stay free of update checks.

## Do's and Don'ts

- Reuse shared keyboard input, palette, and Rich components.
- Keep visible help adjacent to the controls it describes.
- Confirm choices explicitly; arrow movement alone never launches or attaches.
- Render UI on the main thread and keep monitoring independent of network work.
- Verify keyboard cancellation, narrow widths, redirected input, and legacy output.

Dashboard help owns keyboard input while open: scrolling, closing help, and view
selection remain active. Its footer shows those actions only. Target controls and
session actions require closing help first.

The shared Rich Console owns terminal color policy across normal, headless,
and diff output. Explicit `--no-color` wins over forced color; absent that flag,
Rich's environment behavior is retained. Words and emphasis carry verdicts
and unknown readings independently of palette colors.

`--plain` is a static headless text flow using the same destination and labels,
with terminal styles and redraws disabled. `--quiet` suppresses routine output
while retaining warnings, signals, errors, and the complete final summary.
These output choices preserve target ownership, evidence, reports, and exits.
