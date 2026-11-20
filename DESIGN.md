---
version: alpha
name: DrainTracer monitoring and reports
description: A terminal workspace and offline field report for investigating resource growth on Windows and Linux.
colors:
  primary: "#53b7ff"
  accent: "#53b7ff"
  muted: "#adbccc"
  border: "#647d95"
  surface: "#203449"
  danger: "#ff8076"
  ok: "#82e78a"
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

DrainTracer serves developers monitoring Windows and Linux process trees. Preserve the
existing instrument-panel identity: startup artwork from TITLE.txt, blue
accents, quiet metadata, and dense resource views. Product familiarity and
readable measurements guide changes; avoid marketing layouts in the CLI.
The maintained product and workflow references are README.md and docs/usage.md.

## Colors

The terminal palette in src/theme.py supplies startup.py, process_picker.py
and dashboard.py. Primary maps to PALETTE.accent, the water blue from the brand
kit. Trace teal and violet distinguish secondary readings. Signals progress from
green through yellow and coral to pink. The selected startup action uses a
literal `>`, a number and a filled background. Monitoring tabs identify the current page in brackets. Signal
meters distinguish filled and empty cells in monochrome.
The requested update notice uses Rich's named red, green, and blue colors for
the availability label, pip command, and repository link respectively.

## Typography

The user's terminal controls the monospace font and size. Bold labels identify
actions, muted lines explain them, and technical values remain literal. Keep
navigation hints in ASCII and preserve legacy Windows encoding fallbacks.

## Layout

Startup groups machine-wide CPU and RAM together with their measurement time.
The target choices and keyboard help sit above the four process rankings.
Wide terminals place the rankings in two columns. Narrow terminals stack
them and page the results. Labels and numeric readings accompany meters.
The terminal controls font metrics and the branding falls back to plain text
when the artwork does not fit.

## Elevation & Depth

Borders and selected-row backgrounds provide hierarchy; terminal panels use no
shadows or decorative overlays.

## Shapes

Reuse rich.box.ROUNDED for startup panels and Rich's legacy-console fallback.

## Components

The system overview uses the nonblocking KeyReader. Keys 1 and 2 select
a target action directly. Up/Down and Enter or Space also work. P holds a
frame for reading while the worker continues sampling. Returning to live
shows the latest completed sample. The running-process picker keeps its own
text-entry handling for names and PIDs. Canceling an interactive picker
returns to the overview. Redirected input retains numbered line prompts.

The update status belongs directly below the version subtitle. A confirmed
newer release shows the upgrade command; a confirmed current release shows the
contribution link. Unavailable checks use a distinct status. Headless output,
help, and version-only commands stay free of update checks.

## Do's and Don'ts

Offline HTML reports use the official light and dark DrainTracer wordmarks
from the [brand kit](docs/branding.md). Embed the SVG artwork in the export
so its pipe, droplet, reticle and lettering travel with the report.
Present the target, verdict and evidence as a technical report.
Keep the contents navigation, resource measurements and session
metadata visually distinct. Use the report stylesheet for light, dark,
print, and responsive layouts. Use report.js for optional interactions.
The static headings and evidence tables belong to the HTML renderer.
Native range fields and Apply support keyboard zoom. Text verdicts, readable
chart values, focus outlines and table overflow support different ways of
reading the same evidence.

- Reuse shared keyboard input, palette, and Rich components.
- Keep visible help adjacent to the controls it describes.
- Confirm choices explicitly; arrow movement alone never launches or attaches.
- Render UI on the main thread and keep monitoring independent of network work.
- Verify keyboard cancellation, narrow widths, redirected input, and legacy output.

The Monitor view is scrollable with Up/Down, Page Up/Down and Home/End, so all
diagnostics remain reachable in narrow terminals. Host pressure is labeled as
whole-host context, with unknown readings, sample age and collection cost.

Named-marker input owns keyboard actions until Enter saves or Escape cancels.
Space inserts label text only during that explicit input state; otherwise Space
retains target pause/resume. Timeline scope uses the process inspector's selected
PID and creation time. Range controls stay within bounded retained history; freezing
captures the current scope and never stops sampling. HTML provides the same named
annotations as visible chart labels and an escaped textual list.

Compare is view 6. It uses the shared strict report-diff engine and renderer,
with comparability and caveats above differences. Baseline path input owns keys
until Enter or Escape; background loading never controls the target. Comparison
results are captured on request, show their capture time, and refresh with R.
The scrollable view preserves the previous result after a failed load.

Repeated report comparisons display strict comparability and run-specific caveats
before resource differences. They use medians with observed ranges, state the
conservative decision rule, and preserve unknown values. Text remains readable
without color and wraps at narrow terminal widths.

Dashboard help owns keyboard input while open: scrolling, closing help, and view
selection remain active. Its footer shows those actions only. Target controls and
session actions require closing help first.

Checkpoint and recovered reports show a partial-evidence verdict using the
existing report badge, panel, and text styles. They name the saved prefix and
unconfirmed tail, final outcome, and cleanup. A recovered recording never gets
an unqualified full-session CLEAR verdict; recorded signal tiers remain visible.

The shared Rich Console owns terminal color policy across normal, headless,
and diff output. Explicit `--no-color` wins over forced color; absent that flag,
Rich's environment behavior is retained. Words and emphasis carry verdicts
and unknown readings independently of palette colors.

`--plain` is a static headless text flow using the same destination and labels,
with terminal styles and redraws disabled. `--quiet` suppresses routine output
while retaining warnings, signals, errors, and the complete final summary.
These output choices preserve target ownership, evidence, reports, and exits.
