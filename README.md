# Process Resource Leak Detector

A Windows CLI/TUI tool that launches a target executable or script, monitors
its complete process tree, and highlights likely memory, handle, and thread
leaks in real time.

## Features

- Native Windows file picker for `.exe`, `.bat`, `.cmd`, and `.py` targets.
- Displays the repository `TITLE.txt` artwork on startup with a legacy-console fallback.
- Recursive child-process tracking with pause, resume, terminate, and kill controls.
- 500 ms telemetry for RSS, VMS, private bytes, handles, threads, CPU, and I/O rates.
- Sliding-window leak heuristics with Low, Medium, High, and Critical severity levels.
- Rich neon telemetry dashboard with live gauges, trend sparklines, diagnostics, and event log.
- JSON session export through the dashboard.

## Quick Start

Requirements: Windows 10/11 and Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python src/main.py
```

The application opens the native file picker on startup. For repeatable runs,
provide a target directly:

```powershell
python src/main.py --target C:\path\to\worker.exe
python src/main.py --target C:\path\to\worker.py -- --iterations 10
```

## Dashboard Controls

| Key | Action |
| --- | --- |
| `Space` | Pause/resume monitoring and the target process |
| `K` | Kill the monitored process tree |
| `E` | Export the session to `leak_report.json` |
| `Q` | Quit and cleanly terminate a running target |

## Project Layout

```text
src/
├── main.py             # CLI entry point and picker workflow
├── dashboard.py        # Rich terminal UI and controls
├── events.py           # Snapshot models and MetricsStream
├── file_picker.py      # Native Windows file selection
├── process_runner.py   # Process lifecycle and PID tree tracking
└── leak_detector.py    # Sampling loop and leak heuristics
```

## License

MIT License
