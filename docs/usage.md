# Usage

[README](../README.md) · [Configuration](configuration.md) · [Reports](reports.md) · [Working principles](../working-principle.md)

Run the commands below from the repository root after installing
`requirements.txt` into a Python 3.10+ environment on Windows 10/11.
DrainTracer currently runs from source; the command is `python src/main.py`.

## Startup file check and recovery

Every launch checks the installation against the committed
`runtime-manifest.json` before importing the application or starting a
target. The snapshot includes runtime modules, `TITLE.txt`, requirements,
and the reusable report CSS/JavaScript. It does not require generated
reports, caches, a virtual environment, or your `.drainrc` file. Paths are
checked relative to the installation, independent of the working directory.

If required files are missing, the CLI lists their paths. Press **Enter**
to run `git pull --ff-only origin main` in that checkout. After a successful
pull, DrainTracer restores only absent required files that Git tracks,
checks again, and restarts with the same Python, arguments, and working
directory. This also repairs deleted files when Git says the branch is
already up to date. Existing files, staged deletions, and directories that
occupy a required file's path are not overwritten by the restoration.

Press **Q** or close input to cancel with exit code `2`; Ctrl+C exits `130`.
Git must be installed, the source must be a Git checkout with an `origin`
remote, and the pull must be able to fast-forward. Recovery stops on Git
errors or if files are still missing; it does not reset or switch branches.
An invalid snapshot needs review or restoration from Git before startup.

Headless mode and redirected input never prompt or pull automatically;
an incomplete installation exits `2` with instructions. Missing Python
packages instead show a command to install `requirements.txt` using the
current Python. A Git pull does not install Python dependencies.

## Choose a target

Start without arguments to choose how to monitor a program:

```powershell
python src/main.py
```

Enter `1` for **Open a program**, which opens the native Windows file picker,
or `2` for **Hook to a running program**, which shows a paged list of
running program names and PIDs, with at most **15 programs per page**
(fewer in a short terminal). In an interactive Windows terminal, use
**Up/Down** to move the `>` highlight, then **Enter** to select that program.
Moving past a page boundary shows the next or previous page automatically.
You can also type a listed PID and press Enter to select it directly.

The list filters live as you type a name or PID; no Enter is needed to
search. Backspace broadens the results immediately, and deleting the last
character restores the full list. Names are matched without case
sensitivity; programs with the same name keep their separate PIDs.
Enter after a name selects the highlighted match. Enter after digits
selects that exact PID, even if another partial match is highlighted.
Arrow navigation keeps the filter and selects among its results; typing
again starts a replacement search, and Backspace edits the existing filter.
Press Escape to cancel. Redirected or non-terminal input retains the
line-based PID/search workflow, where Enter submits a search and empty
lines do not select a program.

- `/search TEXT`: filter names or PIDs, including partial numeric searches
  such as `/search 424`. Its text also filters live in an interactive
  terminal. Bare digits select an exact listed PID.
- `/next` and `/prev`: move between pages.
- `/refresh`: reload running programs while keeping the search.
- `/clear`: remove the search and return to the first page.
- `Q` or `/quit`: cancel the picker. Use `/search q` to search a reserved name.

Slash commands run on Enter and keep the active filter while their command
name is being typed. Typing `/` starts a command without appending it to an
existing search. Refresh reloads the inventory; typing only filters that
snapshot and does not repeatedly rescan the system.

The list omits DrainTracer itself and the idle PID. An exited or changed
selection refreshes the list. Processes whose identity cannot be read are
labeled; use explicit `--pid` if you intend to attempt direct attachment
to one of those processes. Enumeration failures offer refresh instead of
appearing as an empty successful list.

Type `Q` at the startup menu to cancel. Ctrl+C exits with code `130`;
closing the file picker also cancels without starting a target.

The hook choice uses the same attachment behavior as `--pid`: it leaves the
program running when you quit. Arguments after `--` apply only to **Open a
program**; choosing **Hook to a running program** with launch arguments is
a usage error. Explicit `--target` and `--pid` skip the startup menu;
headless mode still requires one of those flags.

Or pass a path directly. Quote paths that contain spaces:

```powershell
python src/main.py --target "C:\work\My App\worker.exe"
python src/main.py --target .\torture\memory_leak.py
python src/main.py --target .\torture\memory_leak.py -- --duration 30 --max-mb 192
```

Arguments after `--` belong to the target. Keep DrainTracer options before
that separator. Python scripts use the current Python interpreter; `.bat`
and `.cmd` scripts use `cmd.exe`; executables launch directly. Launched
targets have standard input, output, and error redirected to the null
device: they cannot read dashboard keys, and their console output is not
captured. Use the target application's own file logging when needed.

Quitting a launch session terminates its tracked process tree. A timeout
does the same. Descendants already discovered remain tracked when the root
exits; extremely short-lived intermediate parents may be missed between
process-tree refreshes.

## Attach to a running process

```powershell
python src/main.py --pid 4820
```

Replace `4820` with the PID shown in Task Manager. DrainTracer tracks the
root and its descendants. `--pid` and `--target` are mutually exclusive;
target arguments after `--` cannot be used with attachment.

Quitting, Ctrl+C, or reaching the timeout detaches and leaves an attached
target running. If DrainTracer paused it, detaching resumes it first.
`K` explicitly kills the attached tree and requires a second press within
three seconds. Attaching to an ancestor such as DrainTracer's terminal
disables pause and kill and excludes the monitor's own branch.

Protected processes and processes owned by another user may expose only
partial metrics. Run elevated only when the target requires those access
rights; unavailable readings do not establish that resource usage is zero.

## Read and control the dashboard

The title shows session state, target, process count, uptime, sample count,
and a countdown when a timeout is set. Metric cards show CPU, RSS, handles,
and threads with trends; private bytes, virtual memory, I/O rates, and
handle categories provide context. The process table and event log help
identify which processes and transitions explain a change.

- `Space`: pause or resume both sampling and the target process tree.
- `K`: terminate the monitored tree; press twice for an attached tree.
- `E`: export a new JSON report and HTML companion.
- `O`: open the latest exported HTML report in the default browser.
- `S`: cycle the process table through tree, memory, handles, and CPU order.
- `?` or `H`: show help and the active monitoring settings; `Esc` closes it.
- `Q`: end the session and clean up or detach according to its launch mode.

Pausing keeps the last totals and alert visible without adding trend
samples. CPU is summed across the tree relative to one core, so it can
exceed 100%. The memory card's headline is RSS; the memory leak heuristic
uses private bytes. A warning identifies sustained resource growth that
needs investigation; it does not identify an allocation or prove a leak.

The dashboard adapts to narrow and short terminals and uses ASCII fallbacks
for legacy encodings. A real interactive terminal is needed for controls;
use headless mode when redirecting output or running in CI.

## Exclude startup growth

```powershell
python src/main.py --target .\torture\startup_spike.py --warmup 5s
python src/main.py --target "C:\work\server.exe" --warmup 15s --timeout 2m
```

Warm-up samples still populate charts and peaks, while leak detection waits.
When warm-up finishes, detection starts with empty windows and a new
baseline. Paused time does not advance the warm-up. A timeout at or before
the end of warm-up is rejected; a timeout that leaves too little time to
fill a detection window produces a warning.

The default sample interval is `500ms`, with `10s`, `30s`, and `60s` windows.
The shortest window needs at least four samples spanning 85% of its
duration, about 8.5 seconds after warm-up at the default interval. Allow a
longer representative workload when investigating slower growth.

## Run without a dashboard

```powershell
python src/main.py --headless --target .\torture\steady.py --timeout 15s
python src/main.py --headless --pid 4820 --timeout 10m --fail-on medium
```

Headless mode requires `--target` or `--pid`. It prints events and a progress
line every five seconds, stops at the timeout or when the tracked tree
exits, then exports reports. Without a timeout it waits for the entire
tracked tree to finish. Launch and attachment cleanup follow the same
rules as the dashboard.

The exit code describes DrainTracer's result, not the target's exit code:

- `0`: no observed signal reached `--fail-on` (default `high`). A run that
  never filled a detection window is `INCONCLUSIVE` and also returns `0`.
- `1`: the strongest signal at any point reached the failure threshold,
  even if it subsequently cleared.
- `2`: invalid usage, startup failure, or a monitoring/report failure when
  no higher-priority outcome applies.
- `130`: Ctrl+C when no signal had reached the failure threshold.

A failing signal takes precedence over an interruption or runtime/report
error; interruption takes precedence over runtime/report errors. Check the
printed errors and report as well as the exit code when diagnosing a run.
An HTML companion failure is logged while the JSON report remains usable.

On GitHub Actions (`GITHUB_ACTIONS=true`), DrainTracer emits verdict
annotations and adds resource results to `GITHUB_STEP_SUMMARY` when set.
An existing Windows job can run:

```yaml
- name: Check resource growth
  shell: pwsh
  run: python src/main.py --headless --target .\dist\app.exe --timeout 2m --warmup 15s
```

Install the dependencies earlier in the job and retain `leak_reports/reports/` with
an artifact upload step that runs even when the check fails. Choose a
timeout and workload long enough for detection to run; exit `0` alone does
not distinguish clean from inconclusive.

## Get help and diagnostics

```powershell
python src/main.py --help
python src/main.py diff --help
```

If the picker fails, pass `--target` directly. If a process exits before a
window fills, extend the workload. If normal initialization triggers a
signal, use a warm-up or a workload-specific profile from the
[configuration guide](configuration.md).

Internal diagnostics can be written to a local file:

```powershell
$env:DRAINTRACER_LOG = Join-Path $PWD "draintracer-debug.log"
python src/main.py --target .\torture\steady.py
Remove-Item Env:DRAINTRACER_LOG
```

The log's parent directory must already exist. Reports and diagnostics can
contain local paths and process details; review them before sharing.
