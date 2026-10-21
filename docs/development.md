# Development guide

[README](../README.md) · [Contributing](../CONTRIBUTING.md) · [Working principles](../working-principle.md) · [Torture targets](../torture/README.md)

DrainTracer is a Python 3.10+ application developed for Windows 10/11.
The standard library `unittest` suite covers its shared logic, while the
bounded torture workloads exercise real Windows processes and handles.

## Set up a checkout

From a local clone:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m draintracer --help
.\.venv\Scripts\python.exe src/main.py --help
```

Using the virtual environment's executable directly avoids depending on
PowerShell script activation. The examples below use `python`; activate
the environment or substitute `.\.venv\Scripts\python.exe` consistently.
The editable install adds the `draintracer` command while using the source
checkout for development. `python -m draintracer` works when the environment's
Scripts directory is not on `PATH`. The original source workflow remains
available with `pip install -r requirements.txt` and `python src/main.py`.

## Build and verify distributions

Install the build frontend and create both a wheel and source archive:

```powershell
python -m pip install build
python -m build
```

Build metadata comes from `pyproject.toml` and `setup.py`. The package version
is extracted from `DashboardApp.VERSION` in `src/dashboard.py`; do not add a
second independently maintained version. The build must include all required
runtime modules, `TITLE.txt`, the report CSS and JavaScript, and the packaged
installation inventory. Missing source inputs must fail the build rather
than producing an incomplete package.

Install the wheel into a fresh environment and verify it from a writable
directory outside the checkout. Replace the wheel filename with the one
created under `dist`:

```powershell
python -m venv C:\temp\draintracer-wheel-test
C:\temp\draintracer-wheel-test\Scripts\python.exe -m pip install .\dist\draintracer-5.0.0-py3-none-any.whl
Set-Location C:\temp
.\draintracer-wheel-test\Scripts\draintracer.exe --help
.\draintracer-wheel-test\Scripts\python.exe -m draintracer --help
```

Then monitor a bounded target and export both JSON and HTML. Check startup
artwork, offline charts, report output under the working directory, and
configuration discovery from that directory. Test installation of the
source archive as well; it must be able to build the same complete wheel.
Repository fixtures remain in the checkout, so use their absolute paths
when testing an installed command from elsewhere.

An installed package validates its bundled files and gives pip reinstall
guidance if they are missing. It must not offer Git repair or write generated
reports into its installation directory. The source-checkout entry retains
the committed snapshot and opt-in Git recovery behavior.

Python script targets use the monitor's interpreter. Install DrainTracer in
the target's environment, or select that environment's `python.exe` with
`--target` and put the script path and arguments after `--`.

Building and local installation do not publish a release. PyPI publication
is a separate future step; do not advertise `pip install draintracer` until
a release is actually available there.

## Find the right module

- [main.py](../src/main.py): standard-library startup file check and
  opt-in Git recovery before dependency imports, argument parsing,
  settings resolution, title artwork, and launch/attach dispatch.
- [runtime-manifest.json](../runtime-manifest.json): committed inventory of
  required runtime modules and assets. Update it only when intentionally
  adding, moving, or removing runtime files; exclude documentation, generated
  reports, and caches. The installation tests verify its coverage of the
  source modules.
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

Before and after every change, compare the committed
[`runtime-manifest.json`](../runtime-manifest.json) with the current required
runtime file structure. Update the inventory only for intentional runtime
additions, moves, or removals; documentation, generated output, caches,
environments, and user settings are not runtime requirements. Do not regenerate
the snapshot from an incomplete installation to hide missing files.

Run this snapshot check both before and after editing:

```powershell
python -m unittest discover -s tests -p test_installation.py
```

Then complete the usual verification:

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
editing. Starting at `v5.0.0`, use Semantic Versioning (`MAJOR.MINOR.PATCH`):
PATCH for backward-compatible fixes, MINOR for backward-compatible features,
and MAJOR for incompatible changes only with the repository owner's explicit
approval. Reset lower components when incrementing MINOR or MAJOR.

Advance the version once per published release, which may collect multiple
commits. A commit, push, or merge is not itself a release and does not require
an automatic bump. Prepare the runtime version and a matching newest entry in
[RELEASES.md](../RELEASES.md) together. The README identifies the release
series as `MAJOR.MINOR.0`, so patch releases keep that label unchanged; update
it when MAJOR or MINOR changes. Any release tag must use the exact
runtime/release-entry version.
Headless banners and generated reports derive their full version from the
runtime value, and distribution metadata extracts the same value without
its leading `v`. Preserve historical changelog and roadmap versions.

Update user-facing docs alongside behavior changes, including controls,
configuration, report fields, and process ownership. `TITLE.txt` remains
the terminal startup branding source with a readable fallback. If changing
report serialization, keep schema compatibility and the `diff` loader in
mind; if changing HTML, verify escaping and the inline script/style hashes.

Before publishing a change, run the relevant tests and smoke workloads,
inspect the diff, and confirm generated caches, virtual environments,
logs, and leak reports are excluded. Follow [CONTRIBUTING.md](../CONTRIBUTING.md)
for pull request scope, validation details, and repository-owner authorship.
