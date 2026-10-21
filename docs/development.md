# Development guide

[README](../README.md) · [Contributing](../CONTRIBUTING.md) · [Working principles](../working-principle.md) · [Torture targets](../torture/README.md)

DrainTracer is a Python 3.10+ application developed for Windows 10/11.
The standard library `unittest` suite covers its shared logic, while the
bounded torture workloads exercise real Windows processes and handles.

## Set up a checkout

From a local clone:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src/main.py --help
```

Using the virtual environment's executable directly avoids depending on
PowerShell script activation. The examples below use `python`; activate
the environment or substitute `.\.venv\Scripts\python.exe` consistently.
There is currently no package build step or installed console entry point.

## Find the right module

- [main.py](../src/main.py): argument parsing, settings resolution,
  startup artwork from `TITLE.txt`, and launch/attach dispatch.
- [runtime-manifest.json](../runtime-manifest.json): committed inventory of
  required runtime modules and assets. Update it when adding, moving, or
  removing runtime files; do not include generated reports or caches. The
  installation tests verify its coverage of the source modules.
- [startup.py](../src/startup.py): keyboard startup choices.
- [process_picker.py](../src/process_picker.py) and
  [running_processes.py](../src/running_processes.py): searchable, paged
  process selection, read-only discovery, and selection identity checks.
- [picker_input.py](../src/picker_input.py): immediate Windows arrow/text
  key decoding and interactive-input detection for the process picker.
- [config.py](../src/config.py) and [units.py](../src/units.py): TOML
  discovery, profile layering, validation, and unit parsing/formatting.
- [process_runner.py](../src/process_runner.py): launch and attach,
  descendant discovery, PID identity checks, pause/resume, and cleanup.
- [leak_detector.py](../src/leak_detector.py): background sampling,
  warm-up, rolling windows, trend tests, and severity scoring.
- [process_table.py](../src/process_table.py) and
  [handle_types.py](../src/handle_types.py): Windows process snapshots
  and kernel handle classification with fallbacks.
- [events.py](../src/events.py): frozen snapshot/alert models and the
  bounded `MetricsStream` bridge.
- [session.py](../src/session.py): shared monitoring lifecycle, collected
  telemetry, event history, and report export coordination.
- [dashboard.py](../src/dashboard.py), [theme.py](../src/theme.py), and
  [widgets.py](../src/widgets.py): Rich rendering, controls, layout,
  styles, and encoding-safe glyphs.
- [headless.py](../src/headless.py): log output, CI annotations and
  summaries, automatic export, and verdict exit codes.
- [history.py](../src/history.py), [report.py](../src/report.py),
  [analysis.py](../src/analysis.py), and
  [html_report.py](../src/html_report.py): bounded export history,
  serialization, report analysis, and offline interactive charts.
- [Report frames](../leak_reports/frames/): reusable CSS and JavaScript
  inlined into exported HTML. Generated reports under
  `leak_reports/reports/` stay ignored; do not add sample reports or captures
  to the source frames.
- [report_diff.py](../src/report_diff.py) and
  [diff_command.py](../src/diff_command.py): report loading,
  comparison rules, JSON output, and terminal presentation.
- [file_picker.py](../src/file_picker.py): native Windows selection
  with fallback handling.

## Preserve the runtime boundaries

The detector owns resource sampling on a background thread. The UI polls
snapshots and renders on the main thread. `MonitorSession` shares lifecycle
and export behavior between the dashboard and headless runner. Keep new
metrics and detection work out of the render loop, and keep presentation
choices out of the process manager and detector.

Process termination, access denial, missing paths, PID reuse, and partial
cleanup are normal conditions to handle. Respect the distinction between
launched targets (owned and stopped at shutdown) and attached targets
(released at shutdown). Tests that create processes must clean them up even
when an assertion fails.

Detection requires several samples over a window. Preserve the coverage,
positive-slope, monotonicity, and recovery checks unless deliberately
changing the detection design. Alert tests should establish both a growth
case and a stable or recovering case. Use explicit timestamps for numerical
tests rather than relying on wall-clock delays.

## Run checks

```powershell
python -m compileall -q src
python -m unittest discover -s tests -v
git diff --check
```

Tests use the standard library; no pytest installation is required. Windows
integration tests exercise real launch, attachment, process trees, and
cleanup. Some tests have platform guards. The HTML report script syntax
test runs when `node` is on `PATH`; it skips when Node.js is unavailable.
Run Windows checks before treating changes to Windows behavior as verified.

For a focused configuration or report change:

```powershell
python -m unittest discover -s tests -p test_config.py -v
python -m unittest discover -s tests -p test_report_diff.py -v
python src/main.py --help
python src/main.py diff --help
```

For real process monitoring and exported-report checks:

```powershell
python torture\verify_suite.py --case steady-python --case memory-growth
python torture\verify_suite.py
```

The full suite takes about 90 seconds and verifies growth signals, normal
recovery, launch types, descendants, attachment cleanup, headless exit
codes, report pairs, and a resolved report comparison. Its sampling
settings are deliberately faster than the application defaults. See
[torture/README.md](../torture/README.md) for bounds, expected outcomes,
report retention, and the auditable executable fixture source.

## Versions and release preparation

The canonical runtime version is `DashboardApp.VERSION` in
[src/dashboard.py](../src/dashboard.py). Read its current value before
editing and advance it by exactly `0.1` for each repository change, as
required by [AGENTS.md](../AGENTS.md). Headless banners and generated
reports derive their version from that value. Historical roadmap versions
describe past changes and should remain historical.

Update user-facing docs alongside behavior changes, including controls,
configuration, report fields, and process ownership. `TITLE.txt` remains
the terminal startup branding source with a readable fallback. If changing
report serialization, keep schema compatibility and the `diff` loader in
mind; if changing HTML, verify escaping and the inline script/style hashes.

Before publishing a change, run the relevant tests and smoke workloads,
inspect the diff, and confirm generated caches, virtual environments,
logs, and leak reports are excluded. Follow [CONTRIBUTING.md](../CONTRIBUTING.md)
for pull request scope, validation details, and repository-owner authorship.
