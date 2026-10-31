# DrainTracer torture targets

[README](../README.md) · [Usage](../docs/usage.md) · [Development](../docs/development.md) · [Reports](../docs/reports.md)

This directory contains bounded Windows and Linux workloads. Python scripts
work on both platforms; `.bat`, `.cmd`, and `.exe` fixtures require Windows,
and the Linux verifier also checks a native `/bin/sleep` executable.
Together they cover genuine leak-shaped growth, normal recovery, recursive
children, a root that exits before its child, and a fast non-zero exit.

For a complete installed-CLI sequence, follow the
[install-to-comparison walkthrough](../docs/first-result.md). It uses copied
fixtures and distinguishes judged, signal, and inconclusive evidence.

Run from the repository root on Windows 10/11 or Linux with Python 3.10+ and the
dependencies in `requirements.txt` installed. The fixtures are diagnostic
examples; an observed signal still needs workload context before treating
it as a real application defect. These scripts require no production
application or customer data.

Run `python torture/verify_suite.py` on either platform. The verifier selects
the supported cases automatically, including recursive Python trees on both
systems. On Linux, use the Python from your Linux virtual environment, such
as `.venv/bin/python`; Windows script and executable cases are excluded.

## Safety

The leak fixtures intentionally consume resources, but their defaults are
bounded:

- `memory_leak.py` stops at 128 MiB.
- `handle_leak.py` stops at 800 open files.
- `socket_leak.py` stops at 300 loopback connections (600 sockets).
- `thread_leak.py` stops at 80 worker threads.
- Every long-running target exits after 20 seconds by default.
- `parent_exits.py` leaves a child for DrainTracer to discover, but that child
  self-terminates after 12 seconds if cleanup fails.

Use `Q` or `K` in DrainTracer to stop a run early. Run one growth fixture at a
time on resource-constrained systems.

## Target catalog

- `steady.py` holds a fixed amount of memory, files, and worker threads. It
  should remain clear of leak alerts.
- `memory_leak.py` commits memory at a steady rate and should raise a memory
  alert.
- `handle_leak.py` retains file objects and should raise a handle alert
  attributed to `file` handles.
- `socket_leak.py` retains loopback TCP connections and should raise a handle
  alert attributed to `socket` handles.
- The `memory-growth-tolerated` verifier case runs `memory_leak.py` under the
  `heavy` profile of `examples/drainrc.toml`, whose memory thresholds are
  meant for workloads that legitimately grow fast, and expects no signal.
- `thread_leak.py` retains blocked workers and should raise a thread alert.
- `burst_recovery.py` repeatedly opens and releases handles and threads. Its
  recoveries should prevent a sustained leak alert.
- `startup_spike.py` allocates 96 MiB during a 2.5-second "startup" and then
  holds steady. Without a warm-up the ramp raises a memory signal
  (`startup-spike`); with `--warmup 3s` it stays clean while detection still
  starts afterwards (`startup-spike-warmup`).
- `process_tree.py` creates a stable recursive Python tree. The default depth
  and fan-out produce seven Python processes.
- `parent_exits.py` exits its root after DrainTracer has had time to discover
  the still-running child.
- `process_tree.bat` exercises the batch launch path and wraps
  `process_tree.py`.
- `quick_exit.cmd` exercises the command-script path, starts one short-lived
  child, and intentionally returns exit code 23.
- The `attach-memory-growth` verifier case starts `memory_leak.py` outside
  DrainTracer, attaches with `--pid` semantics, and checks that quitting
  leaves the target running.
- The `headless-memory-fails` and `headless-steady-passes` verifier cases run
  `memory_leak.py` and `steady.py` through the `--headless` runner and check
  its exit code (`1` and `0`) and its automatically exported report.
- `fixtures/steady.exe` exercises direct executable launch with a stable
  .NET Framework workload.

The default detector needs about 8.5 seconds of a 10-second window before it
can emit an alert. Let the growth targets run for at least 10 seconds when
demonstrating the normal dashboard.

## Manual dashboard runs

Run these commands from the repository root:

```powershell
python src/main.py --target .\torture\steady.py
python src/main.py --target .\torture\memory_leak.py
python src/main.py --target .\torture\handle_leak.py
python src/main.py --target .\torture\socket_leak.py
python src/main.py --target .\torture\thread_leak.py
python src/main.py --target .\torture\burst_recovery.py
python src/main.py --target .\torture\startup_spike.py --warmup 5s
python src/main.py --target .\torture\parent_exits.py
python src/main.py --target .\torture\process_tree.bat
python src/main.py --target .\torture\quick_exit.cmd
python src/main.py --target .\torture\fixtures\steady.exe
```

Arguments after `--` are forwarded to the target:

```powershell
python src/main.py --target .\torture\memory_leak.py -- --duration 30 --max-mb 192
python src/main.py --target .\torture\process_tree.bat -- --depth 3 --fanout 2
```

## Automated verification

The verification runner uses the real `DashboardApp`, `HeadlessRunner`,
`ProcessManager`, `LeakDetector`, settings resolution, cleanup path, and
report exporters. It samples every 100 ms with a four-second window so all
cases finish in about 90 seconds.

```powershell
python torture\verify_suite.py
```

Run one or more named cases:

```powershell
python torture\verify_suite.py --case memory-growth
python torture\verify_suite.py --case batch-process-tree --case command-quick-exit
```

Every case also checks that an HTML report was written next to its JSON
report. Reports are temporary by default. Keep them, and open the HTML
pages in a browser, when diagnosis is useful:

```powershell
python torture\verify_suite.py --reports-dir .\torture\reports
```

A successful full run ends with `17/17 cases passed`: sixteen targets plus a
`diff` of the `memory-growth` and `memory-growth-tolerated` reports, which
must show the memory signal as resolved. The runner fails when an
expected alert is absent, a handle alert names the wrong handle type, a
stable/recovering target alerts, a run returns the wrong exit code, descendants are not observed, a report is invalid, or
DrainTracer leaves tracked processes behind.

## Rebuilding the executable

`fixtures/steady.exe` is generated from `exe/SteadyFixture.cs` with the C#
compiler included in Windows .NET Framework:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\torture\build_exe.ps1
```

The executable is checked in so the `.exe` showcase works without a build
step. The source and build script make the binary auditable and reproducible.
