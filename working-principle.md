# DrainTracer Working Principles

DrainTracer runs on Windows 10/11 and Linux with Python 3.10 or newer. It
launches a target process or attaches to a running PID, tracks
its recursive child-process tree, samples resource counters every 500 ms by
default, and publishes immutable snapshots to the terminal dashboard or
headless runner and export layer.

[README](README.md) · [Usage](docs/usage.md) · [Configuration](docs/configuration.md) · [Reports](docs/reports.md) · [Development](docs/development.md)

The detector produces heuristic warnings. An active leak signal means that
the observed resource behavior matches sustained growth criteria; it is not
proof of a specific unreleased allocation, handle, or thread.

## Runtime architecture

`main.py` resolves command-line flags and configuration, then constructs a
`DashboardApp` or `HeadlessRunner`. Both extend `MonitorSession`, which
owns process lifecycle, collected history, events, and report coordination.

The process manager polls the descendant tree and checks PID creation
times. Separately, the detector's background thread samples that tree,
updates rolling detection windows, and publishes frozen `MetricsSnapshot`
objects through `MetricsStream`. The main thread drains that bounded queue
to update session state and render the dashboard or print headless output.
Sampling does not wait for UI drawing. When a queue is full, its oldest
queued item is discarded to allow newer telemetry through.

Alongside the totals, the detector thread keeps a bounded per-process
history (`process_history.py`) used for growth attribution, lifecycle
events, and the process inspector, and emits each detection window's checks
as immutable evidence data. The session keeps exact, fixed-size facts about
every sample (`session_facts.py`) independently of the thinned chart
history. Dashboard views (`view_*.py`) only render this data; none of them
re-evaluates detection, and switching views never pauses sampling.

Every snapshot also carries monitor diagnostics: the requested and achieved
cadence (mean of the last 20 collected samples), the time collection took,
handle-classification age and cost, failed samples, snapshots dropped from
a full queue, and DrainTracer's own CPU (percent of one core), RSS, and
private bytes. These describe how trustworthy the recording is; they are not
inputs to detection.

`analysis.py` supplies common report summaries to the HTML exporter and
report comparison. `report.py` serializes snapshots and writes JSON;
`html_report.py` renders the same report data as an offline page. The
[development guide](docs/development.md) maps the remaining modules.

The monitor reads resource counters and process relationships rather than
instrumenting allocations or inspecting an application's source. A signal
therefore applies to resource growth in the observed tree; it cannot name
the responsible allocation site or distinguish every legitimate cache
from a leak without workload context.

## 1. CPU utilization

For every accessible process, DrainTracer reads the cumulative CPU time
with `process.cpu_times()` and keeps the previous reading per PID:

```text
cpu_percent = (user + system - previous_user_plus_system)
              / seconds_since_previous_reading * 100
```

The first reading of a process establishes its baseline and is reported as
`0.0`. Elapsed time comes from a monotonic clock, so wall-clock adjustments
cannot distort the rate. psutil's own `cpu_percent(interval=None)` is not
used because it stores its baseline on the `Process` object, and tracked
processes are reopened (and identity-checked) on every sample. Nothing
sleeps inside the per-process call, so sampling remains non-blocking.
Baselines for processes that have left the tree are discarded.

The process-tree CPU total is:

```text
total_cpu_percent = sum(process_cpu_percent for every tracked process)
```

Because the total is a sum across processes, it can exceed 100% on a
multi-core machine. The dashboard prints and charts that summed percentage.
CPU is diagnostic telemetry only; it is not an input to the current leak
heuristic.

## 2. Memory usage

DrainTracer reads `psutil.Process.memory_info()` for every tracked process:

- `rss` → resident memory / working set
- `vms` → the platform's virtual-memory counter
- Windows `private` → private committed bytes

On Linux, private memory is USS (Unique Set Size), read through psutil's
`memory_full_info().uss` or `memory_footprint().uss` when that newer API is
available. USS counts resident pages unique to the process. The report's
existing `private_bytes` field holds that value; it does not rename the
schema field or treat Linux USS as equivalent to Windows committed memory.
RSS includes shared pages and is never used as a private-memory fallback.
If `/proc` permissions prevent USS access, private memory alone is marked
unmeasured while readable RSS, descriptor, and thread counters remain usable.
Tree totals sum each measured process field:

```text
total_rss_bytes     = sum(process.rss_bytes)
total_vms_bytes     = sum(process.vms_bytes)
total_private_bytes = sum(process.private_bytes)
```

The memory leak heuristic uses this aggregated private-memory series.
Windows committed private memory and Linux USS are separate definitions:
compare runs from the same platform and check `private_memory_metric` in
the report's experiment environment. RSS is the working set: resident
pages, including shared ones, that the OS can trim or grow without a new
application allocation. The dashboard therefore
charts private bytes in the memory card, compact strip, and process tables
(kept even in narrow tables), with RSS shown as context; memory alerts name
`private_bytes` as their `metric`. Neither counter alone proves an
application-level allocation leak.

Access-denied or unavailable processes keep `0` in their metric fields for
compatibility, but list those counters in `unmeasured`, so the value is
unknown rather than zero. A counter that fails on its own (for example the
handle count) is marked the same way. Totals add up only the measured
processes, and each snapshot's `coverage` records, per metric, how many live
processes were measured and which PIDs were not. Exited processes count as
neither.

## 3. System handles and Linux descriptors

On Windows, each process is sampled with:

```python
process.num_handles()
```

Linux uses `process.num_fds()` instead. Open files, devices, directories,
pipes, sockets, and anonymous kernel descriptors all contribute. Existing
dashboard labels, JSON keys, thresholds, and budget names retain `handles`;
on Linux `--budget-handles N` checks the peak descriptor count against N.
It does not set an OS resource limit. `handle_metric` in the experiment
environment records whether a run measured handles or file descriptors.

The tree count is the sum of the measured per-process counts:

```text
total_handles = sum(process.handles for every measured tracked process)
```

If the platform does not implement the counter, access is denied, or the
process disappears during sampling, the per-process value is reported as
`0` with the counter in `unmeasured`, and coverage records that the value is
unknown. An unknown count does not establish zero resource usage.

The aggregate handle series is one of the three inputs to leak detection.

### Handle types

Every two seconds DrainTracer also classifies each readable process's
handles or descriptors. On Windows, one call to
`NtQuerySystemInformation(SystemExtendedHandleInformation)` returns the
system handle table, whose entries carry the owning PID and an object-type
index; `NtQueryObject(ObjectTypesInformation)` maps indexes to names such
as `File` or `Key`. No handle is ever duplicated into DrainTracer, so
classification needs no access to the monitored processes and cannot hang
on pipe handles the way per-handle name queries can.

Types map to these categories:

| Category | Kernel types |
| --- | --- |
| `file` | `File` (files, pipes, devices), minus sockets |
| `socket` | TCP/UDP sockets owned by the process |
| `registry` | `Key` |
| `event`, `thread`, `process`, `mutex`, `semaphore`, `section` | `Event`, `Thread`, `Process`, `Mutant`, `Semaphore`, `Section` |
| `other` | everything else (ALPC ports, timers, ETW registrations, ...) |

Sockets are `File` objects on `\Device\Afd`, so the per-PID count of open
TCP and UDP connections (from the connection table, which needs no
privileges) is moved from `file` to `socket`. A socket that was never bound
or connected is not in that table and stays counted as a file.

Reading the table takes roughly 20 to 30 ms on a desktop with about 200,000
system handles. When a pass is slower, the next one is scheduled at least
20 times its duration later, keeping classification near 5% of one core.
Processes whose metrics could not be read are left out so the categories
add up to the handle total. After three consecutive failures (for example,
a sandbox that blocks the Windows call), Windows classification turns itself
off and the dashboard falls back to totals.

On Linux, classification reads `/proc/PID/fd` symlinks without opening the
resources they name. Path-backed descriptors are `file`, sockets are
`socket` (including Unix and unconnected sockets), eventfds are `event`,
and pipes or other anonymous descriptors are `other`. The remaining
Windows-specific categories stay zero. A descriptor closed during the pass
is skipped. A PID whose directory or links cannot be read is omitted, and
one protected or exited process does not disable classification for others.
If a pass misses any process whose descriptor total was measured, its
tree-wide type total is unavailable and type-growth history resets, avoiding
a false change when classification access returns. Per-process readable
type counts remain available. Totals and classification run separately, so
resource churn can cause brief differences between them. Slow Linux passes
use the same adaptive interval as Windows passes.

When a handle signal is raised, the alert records `growth_by_type`: the
change per category between the first classification inside the signal's
window and the latest one. Its message names the categories that grew
most, for example `by type: socket +171, file +2`, which separates leaked
file streams from leaked connections.

## 4. Thread count

On Windows, each sample takes one system process snapshot,
`NtQuerySystemInformation(SystemProcessInformation)`, and reads every
tracked process's thread count from it. The same snapshot gives each
process's run state: a process whose threads are all suspended reports
`stopped`. This is the data psutil's `num_threads()` and `status()` read
too, but psutil enumerates every process on the system once per call, so
asking per process cost two full enumerations per tracked process per
sample. If the snapshot is unavailable, DrainTracer falls back to psutil.
Linux directly uses psutil's per-process `num_threads()` and `status()`;
the NT process-table optimization is disabled there.

The reported tree total is:

```text
total_threads = sum(threads of every tracked process)
```

Thread count is sampled as an instantaneous count, not a rate. Sustained
upward growth can indicate thread leakage, thread-pool expansion, or a workload
that is not releasing worker threads. Workload context is still required
before treating the signal as a defect.

## 5. How growth is detected

The detector maintains timestamped rolling series for:

- aggregated private bytes (`memory`)
- aggregated handles (`handles`)
- aggregated threads (`threads`)

The default evaluation windows are 10, 30, and 60 seconds. Samples are kept
for approximately 1.5 times the largest configured window so the oldest
window remains available while new samples arrive.

A window is not evaluated until it contains at least four samples and the time
from the oldest sample to the newest covers at least 85% of the window. This
uses observed timestamps rather than assuming every sample arrives exactly 500
ms apart. A slower sampling loop can therefore still fill a window, while a
short startup burst cannot.

For each eligible window, DrainTracer computes:

### Trend slope

The values are fitted to a least-squares line using each sample's real
timestamp, shifted so the first sample in the window is time zero. The
resulting slope is the estimated resource growth per second:

```text
slope = sum((time - mean_time) * (value - mean_value))
        / sum((time - mean_time)^2)
```

The slope must be greater than zero.

### Monotonic increase ratio

The detector counts adjacent pairs where the next value is greater than or
equal to the previous value:

```text
monotonic_ratio = non_decreasing_pairs / total_adjacent_pairs
```

The current threshold is at least `65%`.

### Drop ratio

The detector tracks the running peak and counts samples that fall below that
peak:

```text
drop_ratio = samples_below_running_peak / total_samples_after_first
```

If the drop ratio is greater than `15%`, the window is treated as showing
meaningful recovery and is rejected. This helps avoid labeling normal
allocation bursts followed by garbage collection or idle cleanup as leaks.

## 6. How active leak signals are detected

A resource/window becomes a candidate active signal only when all of these are
true:

1. The window has at least four samples covering 85% of its duration.
2. Its growth slope is positive.
3. Its monotonic increase ratio is at least `65%`.
4. Its drop ratio is at most `15%`.
5. Its severity score is not `NONE`.

The score is calculated as:

```text
score =
    min(delta / critical_delta, 3.0)
  + min(slope / critical_slope, 3.0)
  + monotonic_ratio
  + duration_bonus
```

The duration bonus is `0.25` for windows at least 30 seconds long and `0.5`
for windows at least 60 seconds long. Shorter windows receive no duration
bonus, including the default ten-second window.

Severity thresholds are:

- `LOW` — score `>= 1.8`
- `MEDIUM` — score `>= 2.5`
- `HIGH` — score `>= 3.5`
- `CRITICAL` — score `>= 4.5`

The critical reference thresholds are resource-specific. These are the
defaults:

- Memory: 50 MiB accumulated growth or 5 MiB/s slope
- Handles: 500 accumulated handles or 20 handles/s slope
- Threads: 50 accumulated threads or 2 threads/s slope

These are normalization references, not hard limits. A persistent but smaller
growth pattern can still produce a low-severity signal. A settings file can
set `critical_delta` and `critical_slope` per resource and per profile; a
profile that changes only one of them keeps the other. The score formula,
the tiers, the monotonic and drop-ratio checks, and the window coverage
rule are the same for every profile.

The sample interval and the windows are configurable too. Every window must
be at least four sample intervals long so it can hold the four samples the
detector requires; settings that break this are rejected at startup.

The detector emits at most one alert per resource in a snapshot: the alert
with the strongest severity across the eligible windows. The snapshot's
overall severity is the strongest severity among its active alerts.

`NONE` means that no current window met all criteria. It does not prove that
the target is leak-free.

### Measurement gaps and evidence states

Each resource series only holds valid observations. A sample where no live
process could be measured for that resource is invalid and clears the
series instead of adding a false zero. The series also starts a new segment
whenever the set of live but unreadable processes changes (access lost or
regained, or an unreadable process starting or exiting), so a window only
spans samples whose totals cover the same processes; access changes cannot
look like growth or a drop. Readable processes that start or exit stay in
the same segment, since their effect on the totals is real. A window
becomes eligible again once the new segment holds four samples covering
85% of it.

Every snapshot carries one `evidence` entry per resource, computed by the
detector: `warming_up`, `collecting` (with the segment's valid sample count
and elapsed time against the four samples and 85% of the shortest window
needed), `no_growth` (with the longest window judged), `growth` (with the
alert's severity and window), `unavailable` (nothing readable), or `stale`
(paused). The dashboard and headless output render these states; they do
not re-derive them.

Each evidence entry also holds one evaluation per configured window: its
point count and covered span, baseline and current value, slope,
monotonic and drop ratios, score, severity, and a verdict: `qualified`, or
the first check it failed (`too_few_samples`, `short_span`,
`no_upward_trend`, `not_monotonic`, `too_many_drops`, or
`below_thresholds`). The dashboard's evidence view (key `3`) shows these values as the
detector computed them, next to the thresholds in force. They stay in
memory and are not exported to reports.

Pausing the monitor does not append another sample and does not clear the last
alert. The paused snapshot repeats the last process totals so the dashboard
does not fall to zero while sampling is stopped.

### Warm-up

With a warm-up (`--warmup` or the `warmup` setting), the sampler runs
normally but feeds nothing to the three leak series and evaluates no window
until that much *active* sampling time has passed. Active time is the sum
of the gaps between consecutive collected samples; the gap that spans a
pause is not counted. Snapshots taken meanwhile carry `warming_up: true` and
no alerts.

The first sample after the warm-up starts the series, so every window
fills with post-warm-up samples only and initialization growth can never
reach one. That sample's totals are kept as the warm-up baseline, reported
in the event log and in the report's `warmup` block.

Separately, the detector notes when its shortest window first holds four
samples covering 85% of its length: before that no signal can be raised at
all. Reports record this as `detection_ran`; a headless run that ends clean
without it is `INCONCLUSIVE`, and its HTML report says so.

### Process attribution and lifecycle

Detection still judges the tree totals. Once an alert is chosen, the
detector asks its per-process history which processes changed the alerted
metric over that alert's own window. Processes are identified by PID and
creation time, so a reused PID is a different process. For each process:

- one that was running when the window began contributes its last value
  minus its first;
- one started inside the window contributes its whole value, since it added
  to the total by arriving;
- one that exited contributes minus its first value, since its resources
  left the total.

Up to five positive contributors are ranked with their share of the positive
growth, and the growth is split into `growth_within_existing` and
`growth_from_new_processes`. The alert message gains "mostly NAME PID N
(+amount)" when one process holds over half, "largest: ..." otherwise, or
"mostly new worker processes (N started in the window)" when new processes
account for most of it. Attribution never changes the alert's severity:
growth from added workers is described as such, but is still raised and can
still fail a headless run.

The history is bounded: point histories for up to 256 processes, 1,024
points each (also trimmed to 1.5x the longest window), summaries of the 64
most recent exited processes, and 64 lifecycle events per snapshot. A start
whose name and parent match a process that exited within the previous ten
seconds is reported as a restart. Snapshots carry `lifecycle` events and
`process_counts`; reports keep live and exited process summaries.

## Process identity and cleanup

DrainTracer records each tracked process's creation time. Before it samples,
suspends, or kills a PID, it opens that PID again and rejects it when the
creation time differs. Both operating systems reuse PID values, so this
check avoids operating on a different process that inherited a stale number.
Windows uses a 10 ms creation-time tolerance; Linux uses a tighter 1 ms
tolerance for timestamps derived from procfs clock ticks. The
root's creation time is kept after the root exits, so a process that later
takes over an attached root's PID is never mistaken for it. Linux zombie and
dead states are treated as exited. Windows also checks whether the process
handle is signaled, since another program may hold a handle after exit even
while basic PID queries still succeed.

DrainTracer never tracks itself. When an attached root is one of its own
ancestors (the terminal, an IDE, or `explorer.exe`), the walk skips
DrainTracer's process and each ancestor below the root, identified by PID
and creation time, together with everything they started. Suspending that
branch would freeze the terminal the dashboard draws in, so pause and kill
are refused for such a tree and only detaching remains.

Launched targets get an empty standard input, so they cannot read or block
on the keys meant for the dashboard. Their standard output and error are
also discarded rather than captured in the event log or reports; targets
that need diagnostic output should use their own file logging. Python `.py`
targets use DrainTracer's Python interpreter. Linux binaries and executable
scripts launch directly; scripts need a valid shebang, or the caller can
explicitly target an interpreter and pass the script after `--`. Windows
executables and batch files require Windows. On Windows, batch files run through
`cmd.exe /d /e:on /v:off /s /c` with every argument quoted for cmd.exe, so
spaces and characters such as `&`, `|`, `<`, `>`, `^`, `!`, and parentheses
reach the script intact instead of splitting the command line. cmd.exe
expands `%NAME%` even inside quotes, before it looks for `&`, so each `%`
is written as `%%cd:~,%`: the empty substring of `%cd%` between it and the
next character keeps any variable name from forming, and an argument such
as `%PATH%` arrives as written. Arguments with line breaks cannot be passed
to a batch file and are refused.

If Ctrl+C arrives while a launched tree is being terminated gracefully,
cleanup switches to an immediate kill (or, for an attached tree, resume and
detach) before the interrupt propagates.

Each refresh takes one `{pid: parent_pid}` snapshot of the system and walks
it breadth-first from every tracked process still running, recording each
new descendant's depth and parent. A child whose creation time is earlier
than its parent's is skipped as a reused PID. (Calling `parent()` per child
would enumerate every system process on each call.)

Windows does not re-parent orphans: a process whose parent exits keeps the
parent's PID as its parent ID. A batch file that hands off with `start`, or
a launcher that starts the real program and exits, therefore leaves
processes that a walk from the living tree cannot reach. For ten seconds
after a tracked process exits, the walk also starts from its PID and adopts
the processes it left behind. A process counts as its child only if it was
created after the parent and before DrainTracer saw the parent gone (and
before any other process took over the PID), so the children of a later
process that reuses the PID are never adopted. A process that starts a
child and exits between two refreshes (500 ms apart) without being seen can
break this parent-link recovery.

Linux reparents orphans, so launches also receive an isolated session with
`start_new_session=True`. Refreshes recover members of that session after
their original parents exit, checking creation times and session-leader PID
reuse before adopting them. This does not change a launched program's
arguments or require a shell. A daemon that calls `setsid()` before any
refresh discovers it can leave both the parent walk and the known session;
that unseen process cannot be guaranteed to remain tracked. Already observed
descendants keep their identity and remain tracked when they change sessions.
Attachment never claims the target's session because it may contain
unrelated programs; it follows discovered descendants and leaves the
attached program running on normal shutdown.

Descendants are terminated before their parents. The tree is resolved first,
then walked from the deepest recorded process upward, so parent links are
still available while cleanup is in progress. Discovered processes remain
tracked after the root exits until they themselves exit or their recorded
identity no longer matches, and the processes they start later are tracked
too.

## Headless runs

`--headless` uses the same session, sampler, and detector as the dashboard;
only the presentation differs. The main thread polls the snapshot stream
every 100 ms and ends monitoring when either:

1. the time limit passes (`--timeout`, wall-clock, counted from the moment
   the target was launched or attached), or
2. the root and every tracked descendant have exited. A child that outlives
   its root keeps the run going until it exits too.

When the tree exits on its own, the sampler is stopped and one final sample
is taken so the exit (and a launched root's exit code) reaches the event log
and the report. Shutdown then terminates a launched tree that is still
running or releases an attached one, and a report is exported.

The exit code is `1` when the strongest signal seen during the run reached
`--fail-on` (default `high`), otherwise `130` after Ctrl+C, `2` when the
report could not be written or monitoring failed unexpectedly, and `0` for
a clean run. Startup failures and usage errors also return `2`. Two
optional policies, off by default, turn otherwise passing runs into
failures: `--fail-on-target-error` returns `4` when a launched root exited
by itself with a non-zero code, and `--fail-on-inconclusive` returns `3`
when no detection window was judged. With a budget
(`--budget-private`, `--budget-handles`, `--budget-threads`, or the matching
settings), a whole-run peak above it returns `5` even without a leak
signal, since stable usage can still be excessive. When several apply, a
leak signal wins, then an interruption, then a monitor or export error,
then a budget, then the target-error and inconclusive policies. Because the
verdict uses the strongest signal seen at any point, a signal that later
clears still fails the run. An unexpected error still stops or releases the
target and exports what was collected before the run exits.

## 7. Leak report structure

Each export writes new files beneath the report root
(`leak_reports/reports/` by default; change it with `--report-dir`), grouped
by the actual target filename and the date in `created_at`. For example,
`worker.exe_2026-10-21/` holds that program's exports on that date. Invalid
Windows filename characters are replaced and component length is bounded.
The filenames embed a random UUID4,
`leak_report_<UUID>.json` and `leak_report_<UUID>.html`, so repeated exports
never overwrite each other. Files are written to a temporary sibling first
and then renamed into place; a failed write removes the temporary file. If
the HTML page cannot be rendered or written, the JSON report is kept and
the failure is logged.

Reusable style/chart source files are committed under `leak_reports/frames/`
and loaded relative to the application source. Their contents are embedded
in each HTML export, preserving offline sharing and CSP hash validation.
Generated reports, including custom-root fixture outputs, are not source
assets; the default generated subtree is ignored by Git. A missing frame
asset fails HTML export without discarding the completed JSON report.

```json
{
  "report_id": "3f2a9c1e-5b7d-4e0a-9c61-0d2f7e8b4a10",
  "report_schema": 3,
  "created_at": "2026-10-13T07:25:21.161+05:30",
  "session_started_at": "2026-10-13T07:25:15.621+05:30",
  "session_ended_at": null,
  "app": "DRAINTRACER",
  "version": "vX.Y.Z",
  "target_path": "C:\\path\\to\\target.exe",
  "root_pid": 1234,
  "mode": "launch",
  "target_exit_code": null,
  "events": ["12:00:00 Launched target.exe · PID 1234"],
  "snapshots": []
}
```

### Top-level fields

- `report_id` — UUID4 that also appears in the filename and in the
  dashboard event log, footer, and exit summary.
- `report_schema` — report layout version. Schema `2` introduced the
  fields above; older reports have no `report_id` and carry a Unix
  `exported_at` instead. Schema `3` added the optional `coverage` and
  `outcome` blocks, snapshot `coverage` and `evidence`, and per-process
  `unmeasured`; readers treat them as absent in older reports.
- `created_at` — ISO 8601 local time, with UTC offset, when the report was
  written.
- `session_started_at` — ISO 8601 time the monitor attached.
- `session_ended_at` — ISO 8601 time monitoring stopped, or `null` when the
  report was exported from a session that is still running (dashboard `E`).
- `app` — application name.
- `version` — exact runtime application version. Releases from `v5.0.0`
  use `vMAJOR.MINOR.PATCH`; historical reports retain their original version
  strings.
- `target_path` — absolute selected target path.
- `root_pid` — root PID; kept after cleanup releases the process.
- `mode` — `launch` or `attach`.
- `target_exit_code` — exit code of a launched root that exited on its own,
  kept when DrainTracer only stopped processes the root left running;
  `null` while it runs, when DrainTracer had to stop it, and for attached
  processes.
- `events` — the most recent dashboard messages, each prefixed with its
  local `HH:MM:SS` timestamp.
- `event_log` — the same messages as objects with `time` (ISO 8601),
  `timestamp` (Unix), `level` (`ok`, `info`, `warn`, `alert`, or `error`),
  and `message`.
- `settings` — the configuration the detector ran with: `profile`,
  `config_path`, `sample_interval_ms`, `windows_seconds`, `warmup_seconds`, `thresholds`
  (per resource, `critical_delta` and `critical_slope`), and
  `timeout_seconds`. Reports from runs with different settings are not
  directly comparable.
- `headless` — present for `--headless` runs: the `fail_on` severity,
  whether it was `breached`, the `fail_on_inconclusive` and
  `fail_on_target_error` policies, and the checked `budgets`.
- `experiment` — optional labels (`workload_id`, `build`, `operations`,
  `note`), mode, duration, sampling settings, budgets, `arguments` reduced
  to a `count` and a 16-character SHA-256 `fingerprint`, and the machine
  (OS, architecture, CPU count, total memory, Python and DrainTracer
  versions), plus `private_memory_metric` and `handle_metric` to identify
  Windows committed-private/handles or Linux USS/file-descriptor semantics.
  Host and user names and environment variables are never
  recorded, and secret-looking label values become `<redacted>`.
- `processes` — final `counts` (live, started, exited, restarted) and
  bounded `live` and `exited` per-process summaries: identity, name,
  parent, first and last seen, exit time, sample count, and first, last,
  peak, and growth of private bytes, handles, and threads.
- `markers` — markers added with `M`: `index`, `label`, `timestamp`,
  `time`, and `elapsed_seconds`.
- `coverage` — session coverage of the signal metrics: `samples` with live
  processes, whether coverage was ever `partial`, per-metric
  `partial_samples` and `unavailable_samples`, `unreadable_pids` (bounded),
  and the `latest` snapshot coverage.
- `outcome` — three separate results: `target` (`state`, `exit_code`,
  `failed`, `description`), `measurement` (`result` of `conclusive`,
  `partial`, or `inconclusive`, `detection_ran`, `partial_coverage`, the
  inconclusive `reason`, and a `coverage` description), and
  `monitor_errors` (distinct sampling, report, and run errors).
- `warmup` — `null` without a warm-up; otherwise its length in `seconds`,
  whether it `completed`, `completed_at` (ISO 8601), and the `baseline`
  totals at that moment.
- `detection_ran` — whether the shortest window ever held enough samples
  to be judged. When it is `false`, the absence of signals means nothing.
- `sample_count` — samples taken during the session.
- `snapshot_stride` — `1` when `snapshots` holds every sample; `N` when a
  long session was thinned to every Nth sample plus every state change.
- `session_facts` — exact, bounded summaries of every sample regardless of
  thinning: `samples` counts by kind, `first_sample_at`/`last_sample_at`,
  `peaks` (`value` and `timestamp` per total), `signals` per resource
  (`worst`, `worst_at`, `episodes`, `escalations`, `signal_seconds`,
  `active`), `evaluation` (`after_warmup_seconds`, `evaluated_seconds`,
  `first_evaluated_at`), `monitor` (cadence, `collection_ms`, monitor CPU
  and memory, handle classification cost and age, failed and dropped
  samples; statistics as `count`/`mean`/`max`), `coverage`, and `chart`
  (`stride`, `stored`, `seen`). Older reports lack it.
- `snapshots` — chronological exported telemetry snapshots. Each may carry
  `diagnostics`: `requested_interval_ms`, `actual_interval_ms` (mean over
  recent samples), `collection_ms`, `handle_types_age_seconds`,
  `handle_types_cost_ms`, `failed_samples`, `dropped_snapshots`, and
  `monitor_cpu_percent`, `monitor_rss_bytes`, `monitor_private_bytes`.

A session keeps up to 7,200 snapshots in memory for export (one hour at
the default interval) and the most recent 200 event messages. A longer
session is thinned instead of cut: when the history fills, every other
snapshot is dropped and only every second new sample is kept, then every
fourth, and so on. Reports therefore span the whole session, from its first
sample to its last. Snapshots where a signal is raised, escalates, eases,
or clears, the warm-up ends, monitoring pauses or resumes, the root exits,
or sampling fails are exempt from thinning up to a limit of 1,800 state
changes. Later changes can be thinned too, keeping memory bounded even
when signals flap on nearly every sample. HTML and comparison summaries
take peaks, worst severities, episode counts, and sample counts from
`session_facts` when present, so thinning changes only the chart and the
growth and drift estimates; older reports fall back to retained snapshots. The
headless failure decision still uses the strongest signal observed during
the session.

### Snapshot fields

Each item in `snapshots` contains:

- `timestamp` — Unix timestamp for the sample.
- `root_pid` — monitored root PID.
- `process_alive` — whether the root process is still running.
- `monitoring_paused` — whether sampling was paused.
- `processes` — per-process metric records.
- `totals` — aggregate process-tree metrics.
- `alerts` — active `LeakAlert` records.
- `severity` — snapshot-wide severity: `none`, `low`, `medium`, `high`, or `critical`.
- `sample_interval_ms` — configured sampling interval.
- `metadata` — tracked PIDs or sampling diagnostics.
- `handle_types` — tree-wide handle or Linux descriptor counts per category
  on complete classification samples; `null` when not due or unavailable.
- `warming_up` — `true` while a warm-up defers the leak heuristics.
- `coverage` — per metric (`rss_bytes`, `private_bytes`, `handles`,
  `threads`): `measured` and `total` live processes and `unmeasured_pids`;
  `null` on failed samples and in reports before schema 3.
- `evidence` — per resource: `state`, `severity`, `valid_samples`,
  `elapsed_seconds`, and `evaluated_window_seconds` (see
  [evidence states](#measurement-gaps-and-evidence-states)).
- `lifecycle` — process `started`, `exited`, and `restarted` events seen in
  this sample, with PID, name, creation time, parent, and the PID a restart
  replaces.
- `process_counts` — live processes and cumulative starts, exits, and
  restarts.
- `diagnostics` — the monitor diagnostics described under
  [runtime architecture](#runtime-architecture).

### Per-process fields

Each `processes` record contains:

```text
pid
name
status
rss_bytes
vms_bytes
private_bytes
handles
threads
cpu_percent
io_read_bytes_per_sec
io_write_bytes_per_sec
access_denied
ppid
handle_types
unmeasured
create_time
```

`create_time` is the process creation time; with `pid` it identifies one
process. `unmeasured` lists the metric names that could not be read for the process;
their fields hold `0` but are unknown. `ppid` is the parent PID recorded while walking the tree; it is `null` for
the root and for processes whose parent was never tracked. `handle_types`
holds the process's handle counts per category on classified samples and is
`null` otherwise.

### Aggregate fields

The `totals` object contains:

```text
rss_bytes
vms_bytes
private_bytes
handles
threads
cpu_percent
io_read_bytes_per_sec
io_write_bytes_per_sec
```

### Alert fields

Each active alert contains:

```text
resource
metric
severity
message
slope_per_second
window_seconds
current_value
baseline_value
growth_by_type
contributors
growth_from_new_processes
growth_within_existing
mostly_new_processes
```

`metric` names the series behind the alert: `private_bytes`, `handles`, or
`threads`. `growth_by_type` is set on handle alerts when handles were
classified during the window (change per category, for example
`{"socket": 171}`) and is `null` otherwise. `contributors` lists up to five
processes (`pid`, `create_time`, `name`, `delta`, `share`, `new_process`,
`exited`) ranked as described under
[process attribution](#process-attribution-and-lifecycle).

Enum values are exported as lowercase strings so reports can be consumed by
simple JSON tooling without importing DrainTracer's Python classes.

## 8. HTML report

The HTML companion is generated from the same report dictionary as the JSON
file, so both always describe the same samples.

- **Verdict and cards.** The strongest severity seen per resource, plus
  growth (last measured value minus first), peak, and drift. Drift is the
  least-squares slope over every measured sample of the run, shown per
  second or, for slow growth, per minute. With a warm-up, growth, drift,
  and resource card peaks start after it, matching what the detector
  judged; overall RSS/CPU peaks and chart traces include startup. A report
  whose detection never ran is `INCONCLUSIVE` rather than clean.
- **Charts.** Private bytes and RSS, handles, handle types, threads, and CPU
  over time. Handle types are plotted on their own time base because they
  are classified every two seconds rather than every sample.
  Hovering shows exact values with a crosshair shared by all charts;
  dragging zooms every chart to the same range; legend entries toggle
  series. A chart line breaks where nothing was measured (paused
  monitoring, a failed sample, or no live process) instead of dropping to
  zero. Shaded bands mark the warm-up, paused periods, and continuous
  signals; dashed lines mark the moment the root process exited.
- **Signal markers.** A red dot marks each sample where a resource's
  signal was raised from clear or escalated to a stronger tier. Easing and
  clearing are not marked. Paused and failed samples neither raise nor
  split a signal.
- **Tables.** One row per continuous signal (strongest severity, start,
  duration, and the alert message when it peaked; the table says when it
  lists fewer episodes than `session_facts` counted), the event log, the
  processes from the last sample that still had live processes, and a
  ranking of growing processes by private-byte growth that keeps exited
  ones.
- **Provenance and diagnostics.** The masthead shows the experiment labels,
  redacted argument count, machine, budgets, evaluated time, whether the
  chart was thinned ("chart thinned 1:N"), and whether the summary facts are
  exact. A Monitor diagnostics panel shows cadence, collection cost, the
  monitor's own CPU and memory, handle-classification cost and age, and
  failed and dropped samples. Sections whose data an older report lacks are
  left out.

The page is a single file with no external requests. Styles, the chart
script, and the data are inline; every value is HTML-escaped, the embedded
JSON escapes `<`, `>`, and `&` so it cannot close its script element, and a
Content-Security-Policy allows only the page's own inline style and script
(pinned by SHA-256) with no network access.

## 9. Report comparison

`diff` loads two JSON reports, summarizes each with the same analysis as
the HTML report, and compares them resource by resource.

**Signals.** The strongest severity each run reached is compared:

| Earlier run | Later run | Status |
| --- | --- | --- |
| none | none | `clear` |
| any | none | `resolved` |
| none | any | `new` |
| any | weaker | `eased` |
| any | stronger | `worsened` |
| any | the same | `persists` |

**Drift.** Each run's drift is the least-squares slope over its measured
samples after any warm-up. A change counts only when it exceeds a noise
band: the larger of 10% of the faster drift and a floor of 1 KiB/s for
memory, 0.01 handles/s, or 0.005 threads/s (each roughly a few units per
hour). Slower drift is `improved`. Faster drift is `regressed` only when
the later run is actually growing faster than the floor; a run that
merely shrinks more slowly is `unchanged`. Big rises are shown as a factor
(`135x`) rather than a percentage.

**Verdict.** A resource improves when its signal resolved or eased or its
drift improved, and regresses when its signal is new or worsened or its
drift regressed. The verdict is `MIXED` when some resources improved and
others regressed, `IMPROVED` or `REGRESSED` when only one direction
occurred, and `UNCHANGED` otherwise. `--fail-on-regression` exits `1` for
`REGRESSED` and `MIXED`.

**Caveats and strict mode.** Every reason the runs may not be equivalent
experiments is a caveat with a kind and, when it is serious enough, the
strict verdict it forces:

| Caveat | Strict verdict |
| --- | --- |
| different targets, workload IDs, launch-argument fingerprints, or detector settings | `NOT COMPARABLE` |
| operation counts more than 10% apart, or recorded by only one run | `NOT COMPARABLE` |
| one run less than half as long as the other | `NOT COMPARABLE` |
| a run that never filled a detection window, or had partial coverage | `INSUFFICIENT COVERAGE` |
| different machines, or a thinned report without exact facts | qualifies only |

Without `--strict`, any caveat qualifies the measured verdict, for example
`IMPROVED (QUALIFIED)`, so a shorter or lighter run cannot earn a clean
result. With `--strict`, a blocking caveat replaces the verdict and the
command exits `3`. The JSON keeps the measured direction in
`measured_verdict` either way.

**Peaks, budgets, and work.** Each resource also shows its whole-run peak
(warm-up included, exact when `session_facts` exists), so stable but
excessive usage is visible without a signal. Budget flags check the later
run's peaks and exit `4` when one is exceeded. When both runs recorded
`--operations`, growth per operation is shown as well.

Reports written before v1.3 have no settings block, before v1.4 no
`detection_ran` field, and before schema 3 no coverage; earlier schema-3
reports may also lack `experiment` and `session_facts`. They are compared
on what they contain.
