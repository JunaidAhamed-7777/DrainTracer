# DrainTracer

DrainTracer is a Windows CLI/TUI tool for watching an
executable or script and its complete child-process tree. It reports live
resource telemetry and raises heuristic warnings when memory, handles, or
threads show sustained growth consistent with a resource leak.

The detector reports *potential* leaks. It is a signal for investigation, not
proof that a particular allocation or handle is permanently unreleased.

## What happens when it runs
0. Read working-principle.md to get a clear and comprehensive idea on how each function works.
1. The application clears the terminal and displays `TITLE.txt`. If the
   terminal uses a legacy code page that cannot display the block artwork, it
   shows the product name instead.
2. On Windows, a native file picker opens for `.exe`, `.bat`, `.cmd`, `.py`,
   and other files. The picker returns an absolute path.
3. `ProcessManager` launches the target and tracks descendants recursively.
   Python files use the current Python interpreter; batch files use
   `cmd.exe /d /e:on /v:off /s /c` with each argument quoted for cmd.exe,
   so `&`, `%NAME%`, and `!NAME!` arrive as written. Targets get an empty
   standard input so the dashboard keeps the keyboard.
4. `LeakDetector` samples the tracked process tree every 500 ms and publishes
   `MetricsSnapshot` objects through `MetricsStream`.
5. The Rich dashboard consumes snapshots on the main thread and renders
   telemetry, alerts, trends, the process tree, and the event log in the
   terminal's alternate screen.
6. On exit, the detector stops, the target process tree is terminated or
   force-killed if it does not exit within the cleanup timeout, and a session
   summary (duration, peaks, worst severity, report path) is printed.

## Quick start

Requirements: Windows 10/11 and Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python src/main.py
```

The normal startup path opens the native file picker. A target can also be
provided directly:

```powershell
python src/main.py --target C:\path\to\worker.exe
python src/main.py --target C:\path\to\worker.py -- --iterations 10
```

### Attaching to a running process

Use `--pid` to monitor something that is already running — a service, a
web server (FastAPI, Node.js), or a database daemon — without restarting it:

```powershell
python src/main.py --pid 4820
```

DrainTracer tracks the process and any descendants it starts. Attached
sessions are handled conservatively:

- `Q` (or Ctrl+C) **detaches**: monitoring stops and the process keeps
  running. A process paused with `Space` is resumed first.
- `K` needs a second press within 3 seconds before it kills an attached tree.
- Processes owned by other users or protected by Windows may show
  `access denied`; run the terminal elevated to read them.
- Attaching to a process DrainTracer itself runs under, such as its
  terminal, an IDE, or `explorer.exe`, leaves DrainTracer and the terminal
  it runs in out of the tree and disables `Space` and `K`, so the dashboard
  can never freeze or end its own terminal.

`--pid` cannot be combined with `--target` or with arguments after `--`.

### Headless mode for CI

`--headless` monitors without the dashboard, for pipelines such as GitHub
Actions. It needs `--target` or `--pid`, prints each event and a statistics
line every five seconds, stops when the time limit passes or the monitored
tree exits, exports a report to `--report-dir`, and sets the exit code:

| Exit code | Meaning |
| --- | --- |
| `0` | No leak signal reached the `--fail-on` severity |
| `1` | A leak signal reached `--fail-on` (default `high`: HIGH or CRITICAL) |
| `2` | Usage error, monitoring could not start or failed unexpectedly, or the report could not be written |
| `130` | Interrupted with Ctrl+C before a failing signal |

```powershell
python src/main.py --headless --target .\build\server.exe --timeout 60s
python src/main.py --headless --pid 4820 --timeout 10m --fail-on medium
```

- `--timeout` accepts `90`, `90s`, `5m`, `1h30m`, or `250ms`. Without it a
  headless run lasts until the process tree exits.
- `--fail-on` takes `low`, `medium`, `high` (default), or `critical`.
- Launched targets are terminated at the time limit; attached targets are
  released and keep running.
- On GitHub Actions (`GITHUB_ACTIONS=true`) the verdict is also emitted as an
  error or warning annotation on the run, and a table of each resource's
  strongest signal, growth, drift, and peak is added to the job summary
  page (`GITHUB_STEP_SUMMARY`).
- A clean run that never filled a single detection window (the tree exited
  or the time limit passed first, warm-up included) is reported as
  `INCONCLUSIVE` with the reason. It still exits `0`, so short smoke runs do
  not fail, but its clean result says nothing about leaks.

A Windows job step:

```yaml
- name: Leak check
  shell: pwsh
  run: python src/main.py --headless --target .\dist\app.exe --timeout 2m
```

Upload `leak_reports/` as a build artifact with `if: always()` to keep the
report when the check fails.

`--timeout` also works with the dashboard: the title bar shows an `ENDS IN`
countdown and the session closes into its summary when the limit passes.

### Warm-up period

Servers, game engines, and JIT runtimes grow quickly while they start: they
load assets, fill caches, and compile hot paths. `--warmup` keeps that
startup growth from reading as a leak:

```powershell
python src/main.py --target .\build\server.exe --warmup 15s
```

During the warm-up DrainTracer samples as usual, so charts, peaks, and
reports are complete, but the leak heuristics wait. Their windows start
empty once it ends, and the totals at that moment become the baseline that
later growth is measured against. Paused time does not count toward the
warm-up. The dashboard shows `WARMING UP` with a countdown, and the event
log records the baseline when detection starts. A `--timeout` that ends
inside the warm-up is rejected, and one too short for a detection window
to fill afterwards prints a warning.

### Settings files and profiles

Per-project settings live in a TOML file. DrainTracer uses the nearest
`.drainrc`, `.drainrc.toml`, or `draintracer.toml` in the working directory
or its parents; a shared `config.toml` counts when it has a `[draintracer]`
table. `--config FILE` picks a file explicitly and `--no-config` ignores
them all.

```toml
sample_interval = "500ms"
windows = ["10s", "30s", "60s"]

[thresholds.memory]
critical_delta = "50MB"     # growth within a window that counts as critical
critical_slope = "5MB/s"    # growth rate that counts as critical

[profiles.heavy]            # python src/main.py --profile heavy --target ...
windows = ["30s", "60s", "2m"]

[profiles.heavy.thresholds.memory]
critical_delta = "4GB"
critical_slope = "1GB/s"
```

| Setting | Meaning | Default |
| --- | --- | --- |
| `sample_interval` | Time between samples, `100ms` to `1m` | `500ms` |
| `windows` | Detection windows: up to 8, each at most `1h` and at least 4 samples long | `10s`, `30s`, `60s` |
| `warmup` | Sampling time before leak detection starts, up to `24h` | none |
| `timeout` | Stop monitoring after this long | none |
| `fail_on` | Headless failure threshold: `low`, `medium`, `high`, `critical` | `high` |
| `report_dir` | Directory for exported reports; a relative path is relative to the settings file | `leak_reports` |
| `thresholds.memory.critical_delta` / `critical_slope` | Memory growth and growth rate that count as critical, as sizes (`50MB`) and rates (`5MB/s`, `300MB/min`) | `50MB`, `5MB/s` |
| `thresholds.handles.*` / `thresholds.threads.*` | The same for handle and thread counts (`500`, `20/s`) | `500`, `20/s`; `50`, `2/s` |
| `default_profile` | Profile applied when `--profile` is not given | none |

A `[profiles.NAME]` table takes the same settings and overrides the base
settings field by field. Flags override both: `--interval`, `--window`
(repeatable), `--warmup`, `--timeout`, `--fail-on`, and `--report-dir`. Unknown or
misspelled settings stop DrainTracer with a suggestion before anything
starts. The dashboard shows the active profile in its leak-health panel,
and every report records the settings it ran with.
[examples/drainrc.toml](examples/drainrc.toml) is a commented example with
`ci`, `heavy`, and `strict` profiles.

Python 3.10 reads TOML through the `tomli` package, which
`requirements.txt` installs there automatically.

## Torture showcase

The [`torture`](torture/README.md) directory contains bounded `.py`, `.bat`,
`.cmd`, and `.exe` targets that demonstrate:

- sustained memory, file-handle, socket, and thread growth;
- normal resource bursts that recover;
- startup growth that a warm-up absorbs;
- recursive process-tree tracking;
- cleanup after the root exits before a child;
- stable and fast-exit baselines;
- headless pass/fail exit codes; and
- a report comparison that sees a signal resolved.

Run every target through the real dashboard, headless runner, and JSON
export path:

```powershell
python torture\verify_suite.py
```

The full suite takes about 90 seconds and exits non-zero if an expected
alert, exit code, process-tree observation, report, or cleanup check fails.

## How active leak signals are detected

Every sample aggregates metrics from the root process and all currently
tracked descendants:

- RSS / working-set memory
- VMS / virtual memory
- Private bytes
- Windows system handle count
- Active thread count
- CPU utilization
- Read and write I/O rates

The dashboard displays all of these metrics. Leak heuristics currently use
the aggregated **private-byte**, **handle**, and **thread** series. CPU and I/O
are diagnostic telemetry, not leak-alert inputs.

### Handle types

Every two seconds DrainTracer also sorts the tree's handles into **file**,
**socket**, **registry**, **event**, **thread**, **process**, **mutex**,
**semaphore**, **section**, and **other** handles, read from the Windows
system handle table without touching the monitored processes. Sockets are
counted from the TCP/UDP connection table and separated from files.

- The handles card shows the largest categories, for example
  `file 1,204 · event 80 · registry 12 · socket 3`.
- A handle signal names the categories that grew, for example
  `by type: socket +171`, so leaked file streams and leaked connections are
  easy to tell apart.
- Reports record the counts per process and for the whole tree, and the
  HTML report charts each category over time.

### Sliding windows

The detector keeps timestamped samples for three default windows:

- 10 seconds
- 30 seconds
- 60 seconds

A window is evaluated only after at least four samples cover 85% of that
window's duration. The slope uses the samples' real timestamps, so a slightly
slower sampling loop can still qualify. This prevents a warning from being
created from only a few startup readings.

### Growth tests

For each resource and each eligible window, the detector calculates:

1. **Trend slope** using least-squares linear regression over the actual sample
   timestamps. The slope must be positive, meaning the resource is growing
   over time.
2. **Monotonic increase ratio**, the proportion of adjacent samples that do
   not decrease. It must be at least 65%.
3. **Drop ratio**, the proportion of samples that fall below the running
   peak. If it exceeds 15%, the window is treated as having meaningful
   recovery activity and is not flagged.

In practical terms, a signal requires sustained upward movement with few
recoveries. A temporary allocation burst that falls back after work completes
should not qualify as an active leak signal.

The detector creates one `LeakAlert` per resource, selecting the strongest
severity found across the eligible windows. Each alert includes:

- Resource type
- Severity
- Current and baseline values
- Growth slope per second
- Evaluation window
- Human-readable diagnostic message

### Severity scoring

Severity combines normalized growth amount, growth rate, monotonicity, and
window duration. The score thresholds are:

- `LOW`: score at least 1.8
- `MEDIUM`: score at least 2.5
- `HIGH`: score at least 3.5
- `CRITICAL`: score at least 4.5

Critical reference thresholds are resource-specific:

- Memory: 50 MB accumulated growth or 5 MB/s slope
- Handles: 500 handles accumulated growth or 20 handles/s slope
- Threads: 50 threads accumulated growth or 2 threads/s slope

These thresholds normalize the score; they are not hard limits. A process can
produce a low-severity signal before reaching a reference threshold when its
growth is persistent and highly monotonic. Settings files can raise or lower
them per project and per profile.

### Important interpretation notes

- Private bytes drive the memory leak heuristic; the dashboard also shows RSS.
- Alerts are based on the aggregate process tree, not only the root PID.
- Access-denied metrics are reported as unavailable rather than guessed.
- Pausing monitoring keeps the last measured totals and alerts, and does not
  add new samples or trend points.
- The CPU percentage is the summed process-tree value, relative to one
  core, so it can exceed 100% on multi-core machines.
- Cleanup validates process creation time before suspending or killing a PID,
  and terminates descendants before their parent.
- A `NONE` severity means no active heuristic signal was found in the current
  sample; it does not prove that the program has no leaks.

## Dashboard layout

- **Title bar** — status pill (`RUNNING`, `WARMING UP`, `PAUSED`, `EXITED`), target, root
  PID, process count, uptime, the `--timeout` countdown when set, and sample
  count.
- **Metric cards** — CPU, memory (RSS), handles, and threads, each with a
  multi-row sparkline and the change over the last 10 seconds. The CPU card
  also shows read/write I/O rates; the memory card shows private and virtual
  bytes; the handles card shows the largest handle types.
- **Process tree** — every tracked process drawn under its parent, with CPU,
  memory, handle, thread, and status columns; `S` switches to a flat list
  sorted by memory, handles, or CPU.
- **Leak health** — the overall signal plus a severity meter and growth
  summary for each resource.
- **Event log** — attach/exit events, pause/kill/export actions, the
  warm-up, and leak signals when they are raised, escalate, ease, or clear.
  Long messages wrap onto a second line.

The layout adapts to the terminal: below 110 columns the panels stack, and
below 30 rows the metric cards collapse into a one-line-per-metric strip.
Terminals whose encoding cannot show Unicode fall back to ASCII boxes and
glyphs.

## Dashboard controls

- `Space` — pause/resume monitoring and the target process
- `K` — kill the monitored process tree (press twice when attached with
  `--pid`)
- `E` — export collected snapshots and events to
  `leak_reports/leak_report_<UUID>.json` plus an interactive
  `leak_report_<UUID>.html`; the UUID appears in the event log, footer, and
  exit summary (`--report-dir` changes the directory)
- `O` — open the latest HTML report in the default browser (shown after an
  export)
- `S` — sort the process table: tree order, or largest first by memory,
  handles, or CPU
- `?` or `H` — show the help overlay: every key, how to read a leak signal,
  and the windows, sampling, warm-up, thresholds, settings source, and report
  folder in effect (`Esc` closes it)
- `Q` — quit and cleanly terminate a launched target (detach and leave it
  running when attached with `--pid`)

When the session ends, the summary lists how the target ended (exit code,
stopped, or left running), the warm-up, the settings source, peaks, the
strongest signal per resource with the handle type that grew, the verdict
(`inconclusive` when no detection window ever filled), and the report
files. If monitoring cannot start, the error comes with a hint, such as
checking the path or PID or running elevated.

## Reports

Every export (and every headless run) writes two files with the same UUID:

- `leak_report_<UUID>.json` — every snapshot, alert, and event, for tools
  and for later comparison. Sessions longer than an hour are thinned
  evenly rather than cut, keeping every signal change, so a report always
  covers the whole session.
- `leak_report_<UUID>.html` — a self-contained page to read or share: the
  verdict, growth and drift per resource, interactive memory, handle,
  thread, and CPU charts with red markers where a leak signal was raised or
  escalated, a table of signals (click one to zoom the charts), the event
  log, and the final process list.

The HTML file needs no network access and loads no external scripts; hover
for exact values, drag across a chart to zoom, and double-click or press Esc
to reset. See [working-principle.md](working-principle.md#8-html-report) for
how each element is derived.

### Comparing reports

`diff` compares two JSON reports, for example from before and after a
fix, or from two releases:

```powershell
python src/main.py diff leak_reports\leak_report_<before>.json leak_reports\leak_report_<after>.json
```

For each resource it shows the strongest signal in each run and how it
changed (`resolved`, `new`, `eased`, `worsened`, `persists`), the drift
(growth rate after any warm-up) in each run and how much it moved, and the
total growth. It ends with a verdict, `IMPROVED`, `REGRESSED`, `MIXED`, or
`UNCHANGED`, and a one-line summary such as
`memory leak signal resolved; memory drift down 99%`. Handle-type growth is
paired when both runs classified handles. Warnings flag comparisons that
may mislead: different targets, different settings, very different run
lengths, or a run that never filled a detection window.

- `--json` prints the comparison as JSON for scripts.
- `--fail-on-regression` exits `1` when the verdict is `REGRESSED` or
  `MIXED`, so CI can compare a branch's report with the main branch's.
- Exit code `2` means a report could not be read.

## Diagnostic log

Errors DrainTracer recovers from, such as a failed sample or a display
error, appear in the event log and the session carries on. To also record
their tracebacks and debug details, for example when reporting a bug, set
`DRAINTRACER_LOG` to a file path:

```powershell
$env:DRAINTRACER_LOG = "drain.log"
python src/main.py --target .\build\server.exe
```

Without it these diagnostics are discarded, so they never draw over the
dashboard or interleave with headless output.

## Tests

```powershell
python -m unittest discover -s tests
```

Unit tests cover the leak heuristics, handle classification, warm-up,
settings files, rendering at each layout breakpoint, the session lifecycle,
headless exit codes, report analysis, HTML escaping, report comparison, and
CLI parsing. The torture suite above exercises real processes.

## Project layout

```text
src/
├── main.py             # CLI entry point, title, and picker workflow
├── config.py           # Settings files, profiles, and precedence
├── session.py          # Monitor lifecycle, collected telemetry, export
├── history.py          # Session-long snapshot history, thinned as it grows
├── dashboard.py        # Rich terminal UI and controls
├── headless.py         # Non-interactive CI runner and exit codes
├── report.py           # Report IDs, timestamps, and atomic JSON writes
├── analysis.py         # Report summaries, signal episodes, chart data
├── html_report.py      # Self-contained interactive HTML reports
├── report_diff.py      # Comparison of two reports: signals and drift
├── diff_command.py     # The diff command's terminal and JSON output
├── stats.py            # Least-squares slope shared with the detector
├── units.py            # Duration, byte, and clock formatting
├── theme.py            # Palette and Unicode/ASCII glyph sets
├── widgets.py          # Sparkline and size-aware renderables
├── events.py           # Snapshot models and MetricsStream
├── file_picker.py      # Native Windows file selection
├── process_runner.py   # Process lifecycle and PID tree tracking
├── process_table.py    # Thread counts and suspended state in one system query
├── handle_types.py     # Windows handle classification by object type
└── leak_detector.py    # Sampling loop and leak heuristics
tests/                  # Unit tests (stdlib unittest)
torture/                # Bounded demonstration and edge-case targets
examples/drainrc.toml   # Commented settings file with example profiles
```

## License

MIT License
