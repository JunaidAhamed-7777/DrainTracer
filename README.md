# DrainTracer

DrainTracer is a Windows CLI/TUI tool for watching an
executable or script and its complete child-process tree. It reports live
resource telemetry and raises heuristic warnings when memory, handles, or
threads show sustained growth consistent with a resource leak.

The detector reports *potential* leaks. It is a signal for investigation, not
proof that a particular allocation or handle is permanently unreleased.

## What happens when it runs

1. The application clears the terminal and displays `TITLE.txt`. If the
   terminal uses a legacy code page that cannot display the block artwork, it
   shows the product name instead.
2. On Windows, a native file picker opens for `.exe`, `.bat`, `.cmd`, `.py`,
   and other files. The picker returns an absolute path.
3. `ProcessManager` launches the target and tracks descendants recursively.
   Python files use the current Python interpreter; batch files use
   `cmd.exe /c`.
4. `LeakDetector` samples the tracked process tree every 500 ms and publishes
   `MetricsSnapshot` objects through `MetricsStream`.
5. The Rich dashboard consumes snapshots on the main thread and renders
   telemetry, alerts, trends, and the event log.
6. On exit, the detector stops and the target process tree is terminated or
   force-killed if it does not exit within the cleanup timeout.

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

### Sliding windows

The detector keeps timestamped samples for three default windows:

- 10 seconds
- 30 seconds
- 60 seconds

A window is evaluated only after it contains enough samples for its duration
at the configured sampling interval. This prevents a warning from being
created from only a few startup readings.

### Growth tests

For each resource and each eligible window, the detector calculates:

1. **Trend slope** using least-squares linear regression. The slope must be
   positive, meaning the resource is growing over time.
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
growth is persistent and highly monotonic.

### Important interpretation notes

- Private bytes drive the memory leak heuristic; the dashboard also shows RSS.
- Alerts are based on the aggregate process tree, not only the root PID.
- Access-denied metrics are reported as unavailable rather than guessed.
- Pausing monitoring produces a paused snapshot and does not add new samples.
- A `NONE` severity means no active heuristic signal was found in the current
  sample; it does not prove that the program has no leaks.

## Dashboard controls

- `Space` — pause/resume monitoring and the target process
- `K` — kill the monitored process tree
- `E` — export collected snapshots and events to `leak_report.json`
- `Q` — quit and cleanly terminate a running target

## Project layout

```text
src/
├── main.py             # CLI entry point, title, and picker workflow
├── dashboard.py        # Rich terminal UI and controls
├── events.py           # Snapshot models and MetricsStream
├── file_picker.py      # Native Windows file selection
├── process_runner.py   # Process lifecycle and PID tree tracking
└── leak_detector.py    # Sampling loop and leak heuristics
```

## License

MIT License
