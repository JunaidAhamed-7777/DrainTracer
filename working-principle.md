# DrainTracer Working Principles

DrainTracer launches a target process, tracks its recursive child-process tree,
samples resource counters every 500 ms, and publishes immutable snapshots to
the terminal dashboard and export layer.

The detector produces heuristic warnings. An active leak signal means that
the observed resource behavior matches sustained growth criteria; it is not
proof of a specific unreleased allocation, handle, or thread.

## 1. CPU utilization

For every accessible process, DrainTracer calls:

```python
process.cpu_percent(interval=None)
```

The first reading establishes psutil's baseline and is reported as `0.0`.
Later readings are the CPU percentage reported by psutil since the previous
sample for that process. The detector does not sleep inside the per-process
call, so sampling remains non-blocking.

The process-tree CPU total is:

```text
total_cpu_percent = sum(process_cpu_percent for every tracked process)
```

Because the total is a sum across processes, it can exceed 100% on a
multi-core machine. CPU is diagnostic telemetry only; it is not an input to
the current leak heuristic.

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

A window is not evaluated until it contains enough samples for its duration at
the configured interval. With the default 500 ms interval this requires
approximately 20, 60, or 120 samples respectively.

For each eligible window, DrainTracer computes:

### Trend slope

The values are fitted to a least-squares line. The resulting slope is the
estimated resource growth per second:

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

1. The window has enough samples.
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

The critical reference thresholds are resource-specific:

- Memory: 50 MiB accumulated growth or 5 MiB/s slope
- Handles: 500 accumulated handles or 20 handles/s slope
- Threads: 50 accumulated threads or 2 threads/s slope

These are normalization references, not hard limits. A persistent but smaller
growth pattern can still produce a low-severity signal.

The detector emits at most one alert per resource in a snapshot: the alert
with the strongest severity across the eligible windows. The snapshot's
overall severity is the strongest severity among its active alerts.

`NONE` means that no current window met all criteria. It does not prove that
the target is leak-free.

## 7. `leak_report.json` structure

The dashboard exports a JSON session envelope:

```json
{
  "app": "DRAINTRACER",
  "version": "vX.Y",
  "target_path": "C:\\path\\to\\target.exe",
  "root_pid": 1234,
  "exported_at": 1789932789.395636,
  "events": ["Attached to target.exe | PID 1234"],
  "snapshots": []
}
```

### Top-level fields

- `app` — application name.
- `version` — runtime application version.
- `target_path` — absolute selected target path.
- `root_pid` — root PID when available.
- `exported_at` — Unix timestamp for report generation.
- `events` — recent dashboard event messages.
- `snapshots` — chronological exported telemetry snapshots.

The dashboard retains up to 600 snapshots and the most recent 8 event
messages in memory for export.

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
```

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
```

Enum values are exported as lowercase strings so reports can be consumed by
simple JSON tooling without importing DrainTracer's Python classes.
