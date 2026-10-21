# DrainTracer

![DrainTracer: see what keeps growing](https://raw.githubusercontent.com/JunaidAhamed-7777/DrainTracer/main/docs/assets/hero.png)

Resource growth monitoring for **Windows 10/11** and **Python 3.10+**.
Launch an executable or script, or attach to a PID, and watch its recursive
process tree in a live terminal dashboard. The target needs no source
changes or instrumentation.

DrainTracer flags sustained growth in private memory, handles, and threads.
A signal is a lead for investigation, not proof of an unreleased allocation.
Process counters do not identify allocation sites, and short runs can be
inconclusive.

## Install and run

From PowerShell:

```powershell
python -m pip install draintracer
draintracer --version
draintracer
```

If the command is not on `PATH`, use `python -m draintracer` with the same
Python used for installation. The startup menu lets you open a program or
select a running process.

```powershell
# Launch a program
draintracer --target "C:\work\My App\worker.exe"

# Attach to an existing process; substitute its actual PID
draintracer --pid 4820

# Allow startup growth before checking a representative workload
draintracer --target .\server.exe --warmup 15s --timeout 2m
```

Quitting stops a launched target and its tracked tree. Quitting an attached
session leaves the target running. Launched targets have their console
input and output redirected; use their own file logging when needed.

Python script targets use DrainTracer's Python interpreter. Install
DrainTracer in the target's environment, or launch that environment's
Python explicitly and put the script after `--`:

```powershell
draintracer --target "C:\work\app\.venv\Scripts\python.exe" -- "C:\work\app\worker.py"
```

## Inspect and save evidence

![DrainTracer dashboard monitoring the bounded memory-growth workload](https://raw.githubusercontent.com/JunaidAhamed-7777/DrainTracer/main/docs/assets/dashboard.png)

Track CPU, memory, handles, threads, and I/O across the discovered process
tree. Use warm-up settings and project profiles to account for startup or
legitimate workload growth. Press `E` to export JSON and an offline
interactive HTML report; press `O` to open the latest report.

Headless mode supports bounded checks, severity-based exit codes, and
GitHub Actions annotations and summaries. Compare saved evidence with
`draintracer diff before.json after.json`. Reports default to
`leak_reports/reports` under the working directory. Review local paths and
process details before sharing exports.

## Documentation and contributions

- [Usage and troubleshooting](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/usage.md)
- [Configuration](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/configuration.md)
- [Reports and comparisons](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/reports.md)
- [Detection and process handling](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/working-principle.md)
- [Release notes](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/RELEASES.md)
- [Source and contributions](https://github.com/JunaidAhamed-7777/DrainTracer)
- [Report a bug](https://github.com/JunaidAhamed-7777/DrainTracer/issues/new/choose)

Released under the [MIT license](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/LICENSE).
