# Development guide

[README](../README.md) · [Contributing](../CONTRIBUTING.md) · [Working principles](../working-principle.md) · [Torture targets](../torture/README.md)

DrainTracer is a Python 3.10+ application for Windows 10/11 and Linux.
The standard library `unittest` suite covers its shared logic, while the
bounded torture workloads exercise real processes, memory growth, Windows
handles, and Linux file descriptors.

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

On Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m draintracer --help
.venv/bin/python src/main.py --help
```

Substitute `.venv/bin/python` for `python` below, or activate with
`. .venv/bin/activate`. Runtime minimums are psutil 5.9.0 and Rich 13.7.0;
Python 3.10 also needs tomli 1.1.0 or newer. The install commands resolve
these from `requirements.txt`. tkinter is optional: desktop selection can
use it, while servers and WSL can use the typed-path fallback, `--target`,
or `--pid` without a display.

Linux CI runs Python 3.10 with those minimum runtime dependencies and
Python 3.14 with current dependencies. Both run regression tests, bounded
workloads, builds, and clean wheel installation; publishing also requires
the Windows checks.

Windows CI runs Python 3.10 with the same minimum runtime dependencies on
`windows-latest`, plus Python 3.14 with current dependencies on
`windows-2022` and `windows-2025`. Every job runs the snapshot, compilation,
JavaScript, regression, bounded real-process, distribution build, and clean
wheel checks. These hosted Windows Server images test the Windows APIs;
they do not substitute for testing every Windows 10/11 desktop configuration.
Jobs have a 20-minute limit. A matrix failure blocks publication, which remains
separate from read-only PR jobs and their three-day failure diagnostics.

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
C:\temp\draintracer-wheel-test\Scripts\python.exe -m pip install .\dist\draintracer-X.Y.Z-py3-none-any.whl
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

The distribution verifier builds a clean environment, runs outside the
checkout, monitors a bounded Python target, and checks both exports on
either operating system. For Linux, use the actual wheel version:

```bash
python tests/verify_distribution.py dist/draintracer-X.Y.Z-py3-none-any.whl
```

An installed package validates its bundled files and gives pip reinstall
guidance if they are missing. It must not offer Git repair or write generated
reports into its installation directory. The source-checkout entry retains
the committed snapshot and opt-in Git recovery behavior.

Python script targets use the monitor's interpreter. Install DrainTracer in
the target's environment, or select that environment's `python.exe` or
Linux `bin/python` with
`--target` and put the script path and arguments after `--`.

The PyPI description comes from [docs/pypi.md](pypi.md). Keep its commands
and feature claims aligned with the README, and use public absolute URLs
for images and documentation links. It is included in the source archive
so metadata preparation works outside a Git checkout.

## Publish to PyPI

Building and local installation do not publish a release. Publication uses
[.github/workflows/publish-pypi.yml](../.github/workflows/publish-pypi.yml)
and PyPI Trusted Publishing. The
[DrainTracer project](https://pypi.org/project/draintracer/) is published on
PyPI, and its trusted publisher is configured.

The publisher uses these exact values:

- **PyPI project name:** `draintracer`
- **GitHub owner:** `JunaidAhamed-7777`
- **GitHub repository:** `DrainTracer`
- **Workflow filename:** `publish-pypi.yml`
- **Environment name:** `pypi`

The matching `pypi` environment exists in the GitHub repository. Manage
the trusted publisher in the PyPI project's publishing settings. See
[PyPI's publisher setup instructions](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
when replacing a publisher or configuring a new project.

Prepare the runtime version and release notes together, complete the
repository checks, and validate both built artifacts with
`python -m twine check --strict`. The release tag must match
`DashboardApp.VERSION` exactly, including its leading `v`. Pushing that
tag starts the publishing workflow. Its manual `workflow_dispatch` entry
accepts a `release_tag` input to retry the same release from its tag.
Published distribution filenames cannot be overwritten with changed
contents; prepare a new release version for changed published artifacts.

After a successful upload, verify the
[PyPI project page](https://pypi.org/project/draintracer/) and install the
published version in a fresh environment from PyPI. Run
`python -m draintracer --version` and a bounded monitoring/export check
outside the checkout. Keep the README's installation guidance aligned
with the published release.

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
- [picker_input.py](../src/picker_input.py): immediate Windows and POSIX
  key reading, arrow/text decoding, terminal restoration, and interactive
  input detection for the startup and process pickers.
- [config.py](../src/config.py) and [units.py](../src/units.py): TOML
  discovery, profile layering, validation, and unit parsing/formatting.
- [process_runner.py](../src/process_runner.py): launch and attach,
  descendant discovery, PID identity checks, pause/resume, and cleanup.
- [leak_detector.py](../src/leak_detector.py): background sampling,
  warm-up, rolling windows, trend tests, and severity scoring.
- [process_table.py](../src/process_table.py) and
  [handle_types.py](../src/handle_types.py): Windows process snapshots
  and kernel handle classification, Linux descriptor classification, and
  psutil fallbacks.
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
- [file_picker.py](../src/file_picker.py): native Windows and optional
  Tk desktop selection; main handles the typed-path fallback.

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

Tests use the standard library; no pytest installation is required.
`python -m unittest discover -s tests -p test_checkpoints.py` also exercises
exclusive-writer crash recovery and a real abruptly killed monitor on the
current platform, using only bounded test-owned workloads. Recovery must never
reopen a target PID or treat a saved identity as process-control authorization.
Integration tests exercise real launch, attachment, process trees, and
cleanup on each platform. Linux checks include shebang execution, descriptor
and USS growth, orphan discovery, and POSIX terminal restoration. Windows
checks retain batch quoting, native handle types, and NT process-table
behavior. Some tests have platform guards. The HTML report script syntax
test runs when `node` is on `PATH`; it skips when Node.js is unavailable.
Pull requests to `main` run the Windows and Linux verification jobs with
read-only repository access. Each job also runs
`node --check leak_reports/frames/report.js` explicitly, so missing Node.js
or invalid report JavaScript fails CI instead of silently skipping it.
The publication job runs only for a release tag or a manual release request;
pull requests do not receive its publishing permissions.
Failed verification jobs upload a diagnostic bundle for three days. It contains
only the last 256 KiB of each snapshot, compilation, JavaScript, regression,
or bounded-workload check's combined output, with a truncation notice when
needed. Logs stream normally to Actions and keep the command's failure status.
The upload uses an explicit log-file allowlist in the runner's temporary
directory; it excludes environment dumps, credentials, and user target reports.
Successful jobs do not upload this bundle. Missing logs produce an upload
warning without hiding the original failed check.
Run both Windows and Linux checks before claiming support for both platforms;
mocked platform tests do not replace native integration tests.

From Windows, test the same checkout through Ubuntu WSL using a separate
Linux environment; do not reuse a Windows virtual environment. For example:

```powershell
wsl -d Ubuntu -- bash -lc 'python3 -m venv ~/.venvs/draintracer-linux'
wsl -d Ubuntu -- bash -lc 'cd /mnt/c/Github/DrainTracer && ~/.venvs/draintracer-linux/bin/python -m pip install -e .'
wsl -d Ubuntu -- bash -lc 'cd /mnt/c/Github/DrainTracer && ~/.venvs/draintracer-linux/bin/python -m unittest discover -s tests'
wsl -d Ubuntu -- bash -lc 'cd /mnt/c/Github/DrainTracer && ~/.venvs/draintracer-linux/bin/python -m compileall -q src'
```

Adjust the distribution and checkout path to the local installation. A
native Linux checkout runs the same `python -m unittest` and compile
commands. Run installation checks before and after changes in both
environments; the committed inventory describes the same runtime files.

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

On Linux, use `python torture/verify_suite.py`; its default cases select
Linux executables and shebang scripts in place of Windows batch/EXE-only
fixtures. Explicit Windows-only cases are unavailable on Linux. Use the
Linux interpreter for WSL runs.

The full suite takes about 90 seconds and verifies growth signals, normal
recovery, launch types, descendants, attachment cleanup, headless exit
codes, report pairs, and a resolved report comparison. Its sampling
settings are deliberately faster than the application defaults. See
[torture/README.md](../torture/README.md) for bounds, expected outcomes,
report retention, and the auditable executable fixture source.

## Host-context collection budget

Run `python torture/benchmark_host.py --samples 200` to measure refresh and cached
passes without launching a target. The script sets a local refresh acceptance
budget to twice the observed p99, rounded up to whole milliseconds with a 1 ms
scheduler floor. This is a measured machine-local budget, not a general observer
overhead claim or a replacement for the broader T5 stress/soak work.

On Windows 11 with Python 3.14.7 on 2026-10-30, 200 passes measured a 0.131 ms
median refresh, 0.362 ms p99, and 0.456 ms maximum; cached passes measured a
0.0032 ms median and 0.0075 ms maximum. The derived 1 ms refresh budget occupies
0.02% of the five-second refresh interval. Refresh timing covers the two host
counter reads; cached timing covers age bookkeeping. Neither uses a blocking
CPU interval. Repeat this benchmark on deployment hardware, especially large
hosts; do not infer Linux costs from this Windows measurement.

Host CPU is percent of total host capacity and is unknown until its second
refresh on the same sampling thread. The first background refresh primes its
own baseline after startup's main-thread sample. Available/total memory is
physical bytes, not Windows commit. Failed
reads remain unknown. Snapshot diagnostics and `session_facts.monitor.host_context`
record age at publication; the live view adds elapsed wall time. The HTML report
labels that saved age at the final sample. Host collection cost is recorded
separately from the target-sampling collection timer.

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

For a preserved installed sharing proof, run
`python tests/run_sharing_proof.py --output NEW_PRIVATE_LEAF` from a native Git
checkout. To reuse the exact Windows-created Git ZIP under Linux/WSL without
reading Windows worktree metadata, supply
`--source-archive SOURCE.zip --source-commit FULL_SHA --archive-sha256 SHA256`
as well. Obtain the hash and commit from the trusted archive producer; the
hash identifies bytes and is not signed source attestation. The helper checks
that hash and the Git ZIP comment before validating all paths/types and
extracting exclusively into a new private leaf. It refuses links, path aliases,
overwrites and excessive archives (64 MiB compressed, 256 MiB expanded,
32 MiB/file, 10,000 entries). Existing attempts remain untouched. Preparation
failures are recorded in `bootstrap.json` and `bootstrap-failure.txt` once the
new private output directory has been provisioned. This command runs real
workloads; lightweight tests are `test_proof_archive.py` and
`test_sharing_proof_bootstrap.py`, which exercise setup without collection.
