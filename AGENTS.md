# DrainTracer Agent Instructions

## Project overview

DrainTracer is a Windows Python 3.10+ CLI/TUI application that launches a
target executable or script, tracks its recursive process tree, samples
resources with `psutil`, and reports potential memory, handle, and thread
leaks through a Rich terminal dashboard.

Core modules are under `src/`:

- `main.py` — CLI entry point, startup title, and file-picker workflow
- `dashboard.py` — Rich UI, controls, event log, and JSON export
- `file_picker.py` — native Windows picker with fallback handling
- `process_runner.py` — process lifecycle and recursive PID tracking
- `leak_detector.py` — background sampling and leak heuristics
- `events.py` — immutable snapshot models and `MetricsStream`

## Mandatory versioning rule

The application started at `v0.1`; initializing this instruction file advances
the current runtime version to `v0.2`. **Every subsequent change to the
repository must increment the application version by exactly `0.1`.** For
example:

```text
v0.1 -> v0.2 -> v0.3 -> v0.4
```

Update the canonical runtime value in `src/dashboard.py`:

```python
DashboardApp.VERSION = "vX.Y"
```

Keep version values in generated reports consistent when those reports are
regenerated. Do not make unrelated versioning or naming changes.

## Implementation guidance

- Preserve the modular backend/UI separation.
- Keep monitoring non-blocking; sampling belongs on the detector background
  thread and UI rendering belongs on the main thread.
- Treat access-denied, terminated-process, missing-file, and partial-cleanup
  states as expected runtime conditions.
- Do not report a leak from a single sample. Preserve the sliding-window and
  monotonic-growth checks unless the detection design is intentionally changed.
- Keep Windows console output safe for legacy code pages.
- Keep `TITLE.txt` as the startup branding source with a readable fallback.
- Update `README.md` when user-visible behavior or controls change.

## Verification and workflow

Before finishing a change:

1. Read the current `DashboardApp.VERSION`.
2. Increment it by exactly `0.1`.
3. Run `python -m compileall -q src`.
4. Run relevant smoke tests and `ReadLints` on edited files.
5. Run `git diff --check` and confirm the working tree state.
6. Commit focused changes frequently with descriptive messages.

Do not commit generated caches, virtual environments, logs, or leak reports;
the repository `.gitignore` already excludes them.
