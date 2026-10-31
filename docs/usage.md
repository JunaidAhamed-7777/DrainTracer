# Usage

Add `--no-color` to monitoring commands or to `draintracer diff` to disable
terminal colors, even when `FORCE_COLOR` is set. Without the flag, Rich keeps
its normal environment and terminal behavior, including `NO_COLOR`.
Textual verdicts and availability labels are preserved; bold emphasis may
remain where the terminal supports it.

For static output, use `--plain --target PATH` or `--plain --pid PID`.
This selects headless monitoring and disables terminal styles and redraws.
Ctrl+C or the time limit finishes the run and exports its reports; ordinary
launch/attachment cleanup rules apply. `--quiet` suppresses routine events,
STAT progress, and the startup banner while keeping warnings, signals, errors,
and the final summary. Combine `--plain --quiet` for a short unstyled transcript.
`draintracer diff BEFORE.json AFTER.json --plain` uses labeled text without
panel borders. Add `--quiet` to keep its verdict, caveats, and budgets only;
`--json` always retains the complete machine result.

[README](../README.md) · [Configuration](configuration.md) · [Reports](reports.md) · [Working principles](../working-principle.md)

Run the commands below from the repository root after installing
`requirements.txt` into a Python 3.10+ environment on Windows 10/11 or Linux.
These examples use the source command `python src/main.py`; an installed
package also provides `draintracer` and `python -m draintracer`.

On Linux, set up an isolated environment from the checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python src/main.py --help
```

If your distribution packages venv support separately, install its Python
venv package first. Use Linux Python inside WSL to monitor Linux processes;
the Windows installation monitors Windows processes.

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

Use **Up/Down** to highlight **Open a program** or **Hook to a running
program**, then press **Spacebar** (or Enter) to select. Navigation help is
below and outside the Get Started box. Press **Q** or **Escape** to cancel.
Redirected or non-terminal input keeps the numbered `1`/`2` prompt.

**Open a program** opens the native Windows file picker on Windows or a
Tk desktop dialog on Linux. Linux does not require a desktop or tkinter:
when no graphical picker is available, enter the target path in the terminal.
You can always bypass the picker with `--target`.

**Hook to a running program** shows a paged list of
running program names and PIDs, with at most **15 programs per page**
(fewer in a short terminal). In an interactive terminal, use
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

On Linux:

```bash
python src/main.py --target /usr/bin/sleep -- 30
python src/main.py --target ./torture/memory_leak.py -- --duration 30 --max-mb 192
chmod +x ./worker.sh
python src/main.py --target ./worker.sh -- "argument with spaces"
```

Arguments after `--` belong to the target. Keep DrainTracer options before
that separator. Python `.py` scripts use the current Python interpreter and
do not need an executable bit. Linux binaries and other scripts launch
directly; scripts need executable permission and a valid shebang such as
`#!/bin/sh`. To run a shell script without executable permission, use
`--target /bin/sh -- ./worker.sh`. `.bat` and `.cmd` files use `cmd.exe` on
Windows; batch files and Windows executables require Windows. Launched
targets have standard input, output, and error redirected to the null
device: they cannot read dashboard keys, and their console output is not
captured. Use the target application's own file logging when needed.

Quitting a launch session terminates its tracked process tree. A timeout
does the same. Descendants already discovered remain tracked when the root
exits; extremely short-lived intermediate parents may be missed between
process-tree refreshes. Launched Linux targets receive their own session,
so children that keep that session can be rediscovered after Linux reparents
them. A daemon that creates a new session before any refresh sees it can
escape discovery; attach to its actual PID if you need to monitor it.

## Attach to a running process

```powershell
python src/main.py --pid 4820
```

Replace `4820` with the PID shown in Task Manager on Windows or `ps` on Linux.
DrainTracer tracks the root and its descendants. `--pid` and `--target` are
mutually exclusive;
target arguments after `--` cannot be used with attachment.

Quitting, Ctrl+C, or reaching the timeout detaches and leaves an attached
target running. If DrainTracer paused it, detaching resumes it first.
`K` explicitly kills the attached tree and requires a second press within
three seconds. Attaching to an ancestor such as DrainTracer's terminal
disables pause and kill and excludes the monitor's own branch.

Protected processes and processes owned by another user may expose only
partial metrics. Run elevated only when the target requires those access
rights; unavailable readings do not establish that resource usage is zero.
On Linux, `/proc` restrictions, a different process owner, or container
boundaries can hide private memory or descriptors independently. Attachment
never adopts unrelated processes merely because they share the target's
Linux session.

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
- `M`: add a numbered marker such as "opened document" timing to the event
  log, timeline, and report.
- `?` or `H`: show help and the active monitoring settings; the arrow and
  paging keys scroll it, and `Esc` closes it.
- `Q`: end the session and clean up or detach according to its launch mode.

Number keys switch between focused views, and `Esc` returns to the
overview (it closes help, or clears a process filter, first). Switching
views never pauses sampling or changes the target.

- `1` Overview: the cards, process tree, leak health, and event log.
- `2` Processes: every live process as a tree, or ranked by growth of
  private bytes, handles, or threads (`S`) over a detection window or each
  process's lifetime (`Left`/`Right`). Recently exited processes stay listed.
  `Up`/`Down` select, `/` filters by name or PID, `Tab` moves focus to the
  detail, and `Enter` shows the detail alone. The detail lists identity and
  lifetime, parent, status, private bytes, RSS, signed growth and slope,
  handles, threads, CPU, I/O, recent lifecycle events, and tree totals. The
  selection follows the process identity (PID and creation time). `K` still
  terminates the whole tree.
- `3` Evidence: for each resource (`Left`/`Right`), the metric, valid sample
  coverage, readable processes, and every window's points, span, baseline,
  current value, delta, slope, monotonic and drop ratios, heuristic score,
  and the check that decided it. These values are the detector's own; the
  view does not recompute them. Severity is a heuristic tier, not a
  probability.
- `4` Timeline: one chart at a time (`Left`/`Right`: private bytes, handles,
  threads, RSS, CPU) over the last 240 samples. Columns show the highest
  sample in their time bucket, pauses leave gaps, and the caption states
  samples per column. Annotations mark warm-up end, process starts, exits
  and restarts, pauses, unreadable processes, signal changes, the active
  window start, and markers. `F` freezes this view only; sampling, detection,
  and the target continue, and `TIMELINE FROZEN` stays visible.
- `5` Monitor: requested and actual cadence, collection time, sample counts,
  dropped snapshots, unreadable processes, evidence and handle-type
  freshness, and DrainTracer's own CPU and memory. Host CPU and memory are
  not collected yet.

Pausing keeps the last totals and alert visible without adding trend
samples. CPU is summed across the tree relative to one core, so it can
exceed 100%. The memory card's headline and chart are private bytes, the
counter the memory leak heuristic uses; RSS is shown as context. A warning identifies sustained resource growth that
needs investigation; it does not identify an allocation or prove a leak.

On Windows, private bytes are private committed memory. On Linux, the
`private_bytes` report field contains USS: resident memory unique to the
process. RSS includes shared resident pages and is never substituted when
private memory is unreadable. These counters measure different things, so
compare runs from the same platform and check the report's environment.

Linux uses open file descriptor counts wherever the dashboard, reports,
configuration, or budgets say `handles`. `--budget-handles 500` therefore
checks whether the tree's peak open descriptor count exceeds 500; it does
not impose an operating-system limit. Type classification reads
`/proc/PID/fd`: files, devices, and directories are `file`; sockets,
including unconnected Unix sockets, are `socket`; eventfds are `event`;
pipes and other anonymous descriptors are `other`. Windows-only categories
stay zero. Classification is sampled separately from total counts and can
differ briefly as descriptors open or close.

The leak health panel shows the current evidence, separate from the
session's worst signal:

- `COLLECTING EVIDENCE 6s/8.5s` (rows: `COLLECT`): the shortest window is
  still filling; no conclusion is possible yet.
- `NO SUSTAINED GROWTH (60s window)` (rows: `STEADY`): a window was judged
  and showed no sustained growth.
- A severity such as `HIGH LEAK SIGNAL`: growth was detected.
- `UNAVAILABLE` (rows: `N/A`): no process in the tree could be read.
- `PAUSED`: evidence is on hold while sampling is paused.
- `PARTIAL: 1 process unreadable`: totals leave unreadable processes out,
  so they are lower bounds. The process table marks those rows `denied`.

Unreadable counters are unknown, not zero. When a process becomes
unreadable or readable again, the affected resource restarts its windows,
so gaining or losing access cannot pass for growth or a drop. The event
log records each change, and reports keep per-sample coverage.

The dashboard adapts to narrow and short terminals and uses ASCII fallbacks
for legacy encodings. A real interactive terminal is needed for controls;
use headless mode when redirecting output or running in CI.

## Preserve interrupted-run evidence

To preserve evidence during a long run, add `--checkpoint PATH` and optionally
`--checkpoint-interval 5s` to either monitoring mode. Use a new checkpoint path
for each session. After interruption, `draintracer recover PATH --report-dir
recovered` exports the saved prefix without controlling any process. The report
remains partial; see [checkpoint recovery](reports.md#checkpoint-recovery) for
bounds, validation, failure behavior, and comparison qualification.

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

The same workflow works on Linux servers and WSL without a desktop:

```bash
python src/main.py --headless --target ./torture/steady.py --timeout 15s
python src/main.py --headless --pid 4820 --timeout 10m --budget-handles 500
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
- `2`: invalid usage or startup failure; after successful startup, a
  monitoring/cleanup exception or JSON-report failure when no higher-priority
  outcome applies.
- `3`: with `--fail-on-inconclusive`, no detection window was judged;
  with `--require-full-coverage`, not every live sample had known full
  private-byte, handle, and thread coverage, or a sample failed. Pauses are
  excluded. The two requirements are independent and can be combined.
- `4`: only with `--fail-on-target-error`: a launched target exited by
  itself with a non-zero code. A target DrainTracer stopped does not count.
- `5`: only with a budget (`--budget-private SIZE`, `--budget-handles N`,
  `--budget-threads N`, or the matching settings): a whole-run peak,
  warm-up included, exceeded it. The event log notes the first breach of
  each budget, and the summary has a `budgets` row.
- `130`: Ctrl+C when no signal had reached the failure threshold.

Startup failure always exits `2` and attempts to export available evidence.
After startup succeeds, a failing signal takes precedence over an interruption or runtime/report
error; interruption takes precedence over runtime/report errors, and those
over `5`, then the optional `4`, then `3`. These policies are off by default and
can also be set with `fail_on_target_error`, `fail_on_inconclusive`, and
`require_full_coverage` in a
settings file. The summary prints the target outcome, the measurement
outcome (`conclusive`, `partial` when some processes were unreadable, or
`inconclusive` with its reason), and monitor errors on separate lines; the
report's `outcome` object also records structured startup and cleanup results.
Cleanup exceptions still attempt to export evidence. Nonthrowing cleanup
survivors retain the default exit behavior but emit a warning and
`outcome.cleanup.state: incomplete`; automation requiring full cleanup must
check that field. See [the report contract](reports.md#startup-and-cleanup-outcomes).
Check the
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

If the desktop picker is unavailable, enter its path at the fallback prompt
or pass `--target` directly. If a process exits before a
window fills, extend the workload. If normal initialization triggers a
signal, use a warm-up or a workload-specific profile from the
[configuration guide](configuration.md).

Internal diagnostics can be written to a local file:

```powershell
$env:DRAINTRACER_LOG = Join-Path $PWD "draintracer-debug.log"
python src/main.py --target .\torture\steady.py
Remove-Item Env:DRAINTRACER_LOG
```

On Linux, set the variable for one invocation:

```bash
DRAINTRACER_LOG="$PWD/draintracer-debug.log" python src/main.py --target ./torture/steady.py
```

The log's parent directory must already exist. Reports and diagnostics can
contain local paths and process details; review them before sharing.
