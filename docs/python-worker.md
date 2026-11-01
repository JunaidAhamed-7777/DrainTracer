# Monitor a Python worker in its own environment

Use the installed CLI from the monitoring environment prepared in the
[first-result walkthrough](first-result.md#install-a-separate-monitoring-environment).
Start in the checkout root, with its `$GuideRoot` and `$Monitor` variables.
The bounded `torture/steady.py` worker is a source-only example; PyPI does not
install it. This recipe needs normal permission to launch your own processes.

Create a **separate** environment for the worker:

```powershell
$WorkerEnv = Join-Path $GuideRoot 'worker-env'
python -m venv $WorkerEnv
$WorkerPython = Join-Path $WorkerEnv 'Scripts\python.exe'
$WorkerScript = (Resolve-Path '.\torture\steady.py').Path
& $WorkerPython -I -c "import importlib.util; print(importlib.util.find_spec('draintracer'))"
```

The probe prints `None`: DrainTracer was installed only in the monitoring
environment. This fixture uses Python's standard library. For your application,
install its own dependencies using `$WorkerPython -m pip`, leaving them out of
the monitor's environment. The monitor launches and samples the worker rather
than importing its modules. Python's `-I` isolates the target from the caller's
Python environment variables and user site directory; remove it only if your
application explicitly needs those paths.

Pass the worker interpreter as the **target executable**, then forward the
script and its arguments after `--`:

```powershell
& $Monitor --headless --no-config --target $WorkerPython --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id python-worker --report-dir (Join-Path $GuideRoot 'python-worker-reports') -- -I $WorkerScript --duration 30 --memory-mb 8 --handles 4 --threads 2
$WorkerExit = $LASTEXITCODE
```

This is the normal CLI's supported argument forwarding, with no dedicated
interpreter flag. The absolute script path also works when its directory
contains spaces; keep it as the single `$WorkerScript` argument. A Windows
virtual environment's `python.exe` may launch another Python process. The
recursive tree includes both; this is expected interpreter behavior.

The workload touches and retains 8 MiB, holds four files open, and exercises
two background workers. Its independent self-limit is 30 seconds; the monitor
stops the launched tree after 16 seconds. The first two seconds warm up, then
the 10-second window needs about 8.5 seconds of valid evidence. Expect judged
coverage without sustained resource-growth signals and exit `0`. If the run
ends too early, this recipe's `--fail-on-inconclusive` returns `3`; inaccessible
counters require checking coverage rather than assuming zero usage.

Reports are exported automatically under `$GuideRoot\python-worker-reports`.
Inspect `detection_ran`, metric coverage, and `session_facts` in JSON or the
matching offline HTML. `private_bytes` is committed private memory on Windows,
RSS is working set, and handle/thread values are counts. Python interpreter
overhead means the whole tree's private bytes exceed the fixture's 8 MiB
allocation. Stable counters over this interval do not prove leak absence.

Ordinary timeout and Ctrl+C clean up the launched interpreter tree. The CLI
closes target stdin and redirects stdout/stderr to the null device; the worker
cannot consume monitoring controls. Applications needing explicit cwd,
environment selection, readiness, exercises, or opt-in bounded log capture
can use [workload recipes](workloads.md). Review reports before sharing.

Checked on 2026-10-31 with Windows build 26300, Python 3.14.7 in both fresh
environments, and a source-installed v5.4.0 candidate. The isolation probe
printed `None`; the 16-second run judged windows, had full coverage and no
signals, exported a JSON/HTML pair, and returned `0`. A creation-time check
confirmed that neither observed interpreter identity remained running after
shutdown. Exact counters depend on interpreter and machine conditions.
