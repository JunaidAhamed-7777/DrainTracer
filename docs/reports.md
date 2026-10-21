# Reports and comparisons

[README](../README.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Report schema](../working-principle.md#7-leak-report-structure)

## Export a session

Press `E` in the dashboard to export the samples collected so far. Each
successful export writes a JSON report and an HTML companion with the same
UUID:

```text
leak_reports/
  leak_report_<UUID>.json
  leak_report_<UUID>.html
```

Press `O` to open the latest HTML export. Dashboard exports are explicit;
headless runs automatically export after monitoring ends. Set
`--report-dir DIR` or `report_dir` in a settings file to change the location.
A fresh UUID prevents repeated exports from overwriting earlier reports.
The event log and session summary show the actual filenames.

Both formats are written through temporary sibling files followed by a
rename. If HTML rendering or writing fails, the JSON report is retained and
the error is logged. An in-progress dashboard export can have
`session_ended_at: null` because monitoring has not ended yet.

## Read the HTML report

Reusable styles and chart code are committed in
`leak_reports/frames/report.css` and `leak_reports/frames/report.js`.
They contain generation assets, not sample measurements. The exporter
locates them relative to the application source and embeds them in each
generated page; they are not required alongside a report when sharing it.

Open the `.html` file directly in a browser. It is self-contained: styles,
charts, and report data are embedded, with no external libraries or network
requests. Sharing the file is enough to share its interactive view.

The report includes:

- A verdict and each resource's strongest observed signal, net growth,
  drift, and peak. `INCONCLUSIVE` means no detection window ever filled.
- Memory, handles, handle categories, threads, and CPU charts. Hover for
  values, drag to zoom, and use legend entries to toggle series.
- Markers where signals appeared or escalated, warm-up and pause bands,
  and the time the root exited.
- Continuous signal episodes, events, and the final available process list.

Growth is the last measured value minus the first. Drift is a regression
slope over measured samples. With a warm-up, growth, drift, and resource
card peaks use post-warm-up samples; charts and the overall RSS/CPU peaks
include startup. Charts break across missing readings rather than
interpreting a paused or failed sample as a measured zero.

The HTML and JSON use the same report dictionary, so their exported samples
agree. Handle classifications are less frequent than the main sampling
interval and use their own timestamps.

## Use the JSON data

The current layout is `report_schema: 2`. The report contains identity and
session timestamps, application version, target and mode, resolved
settings, warm-up status, events, and chronological telemetry snapshots.
Each snapshot contains process records, tree totals, alerts, severity,
availability details, and any handle classifications.

For integrations, inspect `report_schema` and allow additional fields.
Do not treat a missing field in an older report as an observed zero.
Enum values such as resource and severity are lowercase strings. Local
ISO 8601 timestamps include their UTC offset; sample timestamps are Unix
timestamps. See the [full schema and field descriptions](../working-principle.md#7-leak-report-structure)
for exact record contents.

Reports retain bounded history: up to 7,200 snapshots and the most recent
200 event messages. Once full, snapshot resolution is progressively thinned
across the whole session rather than losing the beginning. Important state
changes are preserved up to a bounded allowance; `snapshot_stride` records
the thinning. Long reports are therefore not guaranteed to contain every
sample or every event. HTML and comparison summaries are calculated from
retained snapshots, so thinning can change their peaks and drift estimates
relative to the complete live history. The headless failure decision uses
the strongest signal observed during the session.

The target's console output is not included. Target paths, process names,
PIDs, timestamps, settings paths, and event messages may reveal information
about the local environment. Inspect a report before posting it publicly.

## Compare two runs

Run the same representative workload before and after a change, export both,
then pass the earlier report first:

```powershell
python src/main.py diff .\before.json .\after.json
python src/main.py diff .\before.json .\after.json --json
python src/main.py diff .\before.json .\after.json --fail-on-regression
```

Replace those paths with the actual exported JSON files. HTML reports are
for reading; `diff` accepts JSON reports. The comparison shows the strongest
signal, drift, and net growth for memory, handles, and threads, plus handle
category changes when available.

Signal changes are `clear`, `resolved`, `new`, `eased`, `worsened`, or
`persists`. Drift changes use a noise band so tiny fluctuations do not
automatically count as regressions. The combined verdict is `IMPROVED`,
`REGRESSED`, `MIXED`, or `UNCHANGED`; a mixed result has both improvements
and regressions across resources or measures.

By default a valid comparison exits `0` regardless of verdict.
`--fail-on-regression` returns `1` for `REGRESSED` or `MIXED`. Unreadable or
invalid reports, or a comparison failure, return `2`.

Comparisons warn about different targets, detector settings, substantially
different durations, and runs that never filled a detection window. Those
warnings matter: a shorter or less demanding test can appear improved
without demonstrating a fix. Keep the workload, machine conditions,
settings, warm-up, and duration as consistent as practical. Compare reports
from before v1.3 or v1.4 with care: they may lack settings or detection
coverage fields.
