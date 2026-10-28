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

![Demo commands: pip install draintracer, then draintracer --target .\torture\memory_leak.py](https://raw.githubusercontent.com/JunaidAhamed-7777/DrainTracer/main/docs/assets/install-and-launch.gif)

*Install-and-launch commands from the recorded demo. The bounded `torture`
workload requires a [source checkout](https://github.com/JunaidAhamed-7777/DrainTracer#try-an-included-workload);
it is not included in the PyPI package.*

## Inspect and save evidence

![Private memory grows in a child process while DrainTracer collects evidence, then raises a HIGH leak signal](https://raw.githubusercontent.com/JunaidAhamed-7777/DrainTracer/main/docs/assets/live-growth.gif)

*A real session monitoring the bounded memory-growth workload, sped up 1.5×.
The GIF excerpts were recorded with v5.1.1; values and process IDs vary by run.*

Track CPU, memory, handles, threads, and I/O across the discovered process
tree. Use warm-up settings and project profiles to account for startup or
legitimate workload growth.

Leak health says whether evidence is still being collected, whether a
window showed no sustained growth, and whether any process could not be
measured. Memory alerts are based on private bytes, shown alongside RSS
(working set). Alerts name the processes that grew. Number keys open a
process inspector, the evidence behind each signal, an annotated
timeline, and monitor diagnostics.

Press `E` to export JSON and an offline interactive HTML report; press `O`
to open the latest report.

![The exported HTML report shows the HIGH memory signal, resource trend charts, recorded events, and growing processes](https://raw.githubusercontent.com/JunaidAhamed-7777/DrainTracer/main/docs/assets/html-report.gif)

*The offline HTML report from the same demo session: inspect the summary,
resource trends, events, and process growth after recording.*

Headless mode supports bounded checks, severity-based exit codes, and
GitHub Actions annotations and summaries. Compare saved evidence with
`draintracer diff before.json after.json`; add `--strict` to refuse
verdicts for runs that are not comparable, and record `--workload-id`,
`--build`, and `--operations` to make comparisons defensible. Reports
default to `leak_reports/reports` under the working directory. Review local
paths and process details before sharing exports.

## Privacy and terms

Monitoring and reports stay local unless you or your environment share them.
Interactive startup automatically checks PyPI for updates, sending the
DrainTracer version in its user-agent and exposing the connecting IP address
to PyPI. The check does not upload process data or install an update.
Headless runs, help, version-only commands, report comparisons, and redirected
startup output skip it. There is no dedicated interactive opt-out flag.

Reports and optional diagnostic logs can contain sensitive paths and process
details. They have no built-in expiry or encryption; existing label redaction
does not sanitize the entire JSON or HTML report. Read the
[privacy policy](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/PRIVACY.md),
[terms of use](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/TERMS.md),
and [safe sharing guide](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/privacy.md).
The terms preserve the MIT software license. The project operates no hosted
monitoring or report-storage service.

## Documentation and contributions

- [Usage and troubleshooting](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/usage.md)
- [Configuration](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/configuration.md)
- [Reports and comparisons](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/reports.md)
- [Detection and process handling](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/working-principle.md)
- [Release notes](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/RELEASES.md)
- [Source and contributions](https://github.com/JunaidAhamed-7777/DrainTracer)
- [Report a bug](https://github.com/JunaidAhamed-7777/DrainTracer/issues/new/choose)
- [Legal and policy index](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/LEGAL.md)
- [Third-party notices](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/THIRD_PARTY_NOTICES.md)

Released under the [MIT license](https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/LICENSE).
