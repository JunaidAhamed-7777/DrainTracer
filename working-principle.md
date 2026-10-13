# DrainTracer Working Principles

DrainTracer launches a target process, tracks its recursive child-process tree,
samples resource counters every 500 ms, and publishes immutable snapshots to
the terminal dashboard and export layer.

The detector produces heuristic warnings. An active leak signal means that
the observed resource behavior matches sustained growth criteria; it is not
proof of a specific unreleased allocation, handle, or thread.

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
- `vms` → virtual memory size
- `private` → private bytes when the platform exposes it

If `private` is unavailable, the implementation falls back to RSS for that
process. Tree totals are calculated by summing each process field:

```text
total_rss_bytes     = sum(process.rss_bytes)
total_vms_bytes     = sum(process.vms_bytes)
total_private_bytes = sum(process.private_bytes)
```

The dashboard displays RSS as its main memory usage value. The memory leak
heuristic uses the aggregated private-byte series because private bytes more
closely represent memory owned by the monitored process tree.

Access-denied or unavailable processes contribute zero-valued metrics and are
marked with `access_denied` or an unavailable status rather than being
silently treated as a measured value.

## 3. System handles

On Windows, each process is sampled with:

```python
process.num_handles()
```

The tree handle count is the sum of the accessible process handle counts:

```text
total_handles = sum(process.num_handles() for every tracked process)
```

If the platform does not implement the counter, access is denied, or the
process disappears during sampling, the per-process value is reported as
`0` and the snapshot retains the process availability information.

The aggregate handle series is one of the three inputs to leak detection.

### Handle types

Every two seconds DrainTracer also classifies each readable process's
handles by kernel object type. One call to
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
a sandbox that blocks the call), classification turns itself off and the
dashboard falls back to totals.

When a handle signal is raised, the alert records `growth_by_type`: the
change per category between the first classification inside the signal's
window and the latest one. Its message names the categories that grew
most, for example `by type: socket +171, file +2`, which separates leaked
file streams from leaked connections.

## 4. Thread count

For every accessible process, DrainTracer calls:

```python
process.num_threads()
```

The reported tree total is:

```text
total_threads = sum(process.num_threads() for every tracked process)
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

The duration bonus is `0.25` for a 30-second window and `0.5` for a
60-second window. Ten-second windows receive no duration bonus.

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

Pausing the monitor does not append another sample and does not clear the last
alert. The paused snapshot repeats the last process totals so the dashboard
does not fall to zero while sampling is stopped.

## Process identity and cleanup

DrainTracer records each tracked process's creation time. Before it samples,
suspends, or kills a PID, it opens that PID again and rejects it when the
creation time differs by more than one second. Windows reuses PID values, so
this check avoids operating on a different process that inherited a stale
number.

Each refresh takes one `{pid: parent_pid}` snapshot of the system and walks
it breadth-first from the root, recording every descendant's depth and
parent. A child whose creation time is earlier than its parent's is skipped
as a reused PID. (Calling `parent()` per child would enumerate every system
process on each call.)

Descendants are terminated before their parents. The tree is resolved first,
then walked from the deepest recorded process upward, so parent links are
still available while cleanup is in progress. Previously discovered children
remain tracked after the root exits until they themselves exit or their
recorded identity no longer matches.

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
report could not be written, and `0` for a clean run. Startup failures and
usage errors also return `2`. Because the verdict uses the strongest signal
seen at any point, a signal that later clears still fails the run.

## 7. Leak report structure

Each export writes new files to the report directory (`leak_reports/` by
default; change it with `--report-dir`). The filenames embed a random UUID4,
`leak_report_<UUID>.json` and `leak_report_<UUID>.html`, so repeated exports
never overwrite each other. Files are written to a temporary sibling first
and then renamed into place; a failed write removes the temporary file. If
the HTML page cannot be rendered or written, the JSON report is kept and
the failure is logged.

```json
{
  "report_id": "3f2a9c1e-5b7d-4e0a-9c61-0d2f7e8b4a10",
  "report_schema": 2,
  "created_at": "2026-10-13T07:25:21.161+05:30",
  "session_started_at": "2026-10-13T07:25:15.621+05:30",
  "session_ended_at": null,
  "app": "DRAINTRACER",
  "version": "vX.Y",
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
  `exported_at` instead.
- `created_at` — ISO 8601 local time, with UTC offset, when the report was
  written.
- `session_started_at` — ISO 8601 time the monitor attached.
- `session_ended_at` — ISO 8601 time monitoring stopped, or `null` when the
  report was exported from a session that is still running (dashboard `E`).
- `app` — application name.
- `version` — runtime application version.
- `target_path` — absolute selected target path.
- `root_pid` — root PID; kept after cleanup releases the process.
- `mode` — `launch` or `attach`.
- `target_exit_code` — exit code of a launched root that exited on its own;
  `null` while it runs, when DrainTracer had to stop it, and for attached
  processes.
- `events` — the most recent dashboard messages, each prefixed with its
  local `HH:MM:SS` timestamp.
- `event_log` — the same messages as objects with `time` (ISO 8601),
  `timestamp` (Unix), `level` (`ok`, `info`, `warn`, `alert`, or `error`),
  and `message`.
- `settings` — the configuration the detector ran with: `profile`,
  `config_path`, `sample_interval_ms`, `windows_seconds`, `thresholds`
  (per resource, `critical_delta` and `critical_slope`), and
  `timeout_seconds`. Reports from runs with different settings are not
  directly comparable.
- `headless` — present for `--headless` runs: the `fail_on` severity and
  whether it was `breached`.
- `snapshots` — chronological exported telemetry snapshots.

The dashboard retains up to 7,200 snapshots (one hour at the default
interval) and the most recent 200 event messages in memory for export.

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
- `handle_types` — tree-wide handle counts per category on samples where
  handles were classified; `null` on the others.

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
```

`ppid` is the parent PID recorded while walking the tree; it is `null` for
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
severity
message
slope_per_second
window_seconds
current_value
baseline_value
growth_by_type
```

`growth_by_type` is set on handle alerts when handles were classified
during the window (change per category, for example `{"socket": 171}`) and
is `null` otherwise.

Enum values are exported as lowercase strings so reports can be consumed by
simple JSON tooling without importing DrainTracer's Python classes.

## 8. HTML report

The HTML companion is generated from the same report dictionary as the JSON
file, so both always describe the same samples.

- **Verdict and cards.** The strongest severity seen per resource, plus
  growth (last measured value minus first), peak, and drift. Drift is the
  least-squares slope over every measured sample of the run, shown per
  second or, for slow growth, per minute.
- **Charts.** Private bytes and RSS, handles, handle types, threads, and CPU
  over time. Handle types are plotted on their own time base because they
  are classified every two seconds rather than every sample.
  Hovering shows exact values with a crosshair shared by all charts;
  dragging zooms every chart to the same range; legend entries toggle
  series. A chart line breaks where nothing was measured (paused
  monitoring, a failed sample, or no live process) instead of dropping to
  zero. Shaded bands mark paused periods and continuous signals; dashed
  lines mark the moment the root process exited.
- **Signal markers.** A red dot marks each sample where a resource's
  signal was raised from clear or escalated to a stronger tier. Easing and
  clearing are not marked. Paused and failed samples neither raise nor
  split a signal.
- **Tables.** One row per continuous signal (strongest severity, start,
  duration, and the alert message when it peaked), the event log, and the
  processes from the last sample that still had live processes.

The page is a single file with no external requests. Styles, the chart
script, and the data are inline; every value is HTML-escaped, the embedded
JSON escapes `<`, `>`, and `&` so it cannot close its script element, and a
Content-Security-Policy allows only the page's own inline style and script
(pinned by SHA-256) with no network access.
