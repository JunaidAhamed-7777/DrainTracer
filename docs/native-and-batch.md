# Investigate a native executable or batch launcher

These Windows PowerShell recipes use the installed `$Monitor` and temporary
`$GuideRoot` from the [first-result setup](first-result.md#install-a-separate-monitoring-environment).
Run in the repository root. All fixtures below are **source-checkout assets**;
installing the PyPI package does not install `torture`. No administrator rights
are needed to launch these disposable targets.

## Native executable

`torture/fixtures/steady.exe` is the committed .NET Framework fixture built
from `torture/exe/SteadyFixture.cs`. Its [build instructions](../torture/README.md#rebuilding-the-executable)
allow inspecting and rebuilding it. It requires the Windows .NET Framework
runtime; it is not a Linux executable.

```powershell
& $Monitor --headless --no-config --target '.\torture\fixtures\steady.exe' --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id native-steady --report-dir (Join-Path $GuideRoot 'native-reports') -- --duration 30 --memory-mb 8 --threads 2
$NativeExit = $LASTEXITCODE
```

Everything after `--` goes to the fixture, so its 30-second duration is
separate from the monitor's 16-second timeout. The target holds 8 MiB of touched
allocation and two worker threads; the runtime adds its own memory, handles,
and threads. Its accepted memory/thread arguments clamp at 256 MiB/32 workers,
and its own maximum duration is 300 seconds. The shown run uses the smaller
bounds above. Expect one process, a judged 10-second window after the two-second
warm-up, no sustained-growth signals, and exit `0`.

Read the exported JSON/HTML pair under `$GuideRoot\native-reports`. On Windows,
private bytes are committed private memory, RSS is working set, and handles
and threads are counts. A stable .NET runtime can retain a cache without
releasing it immediately; neither retained memory nor a resource slope alone
proves an allocation leak. Check measurement coverage and repeat with a
representative workload before drawing application conclusions.

## Batch launcher and recursive children

`process_tree.bat` starts `process_tree.py` using `DRAINTRACER_PYTHON`, or
`python` from PATH when unset. Select the disposable worker environment from
the [Python recipe](python-worker.md) so the interpreter choice is explicit:

```powershell
$PreviousPython = $env:DRAINTRACER_PYTHON
$env:DRAINTRACER_PYTHON = Join-Path $GuideRoot 'worker-env\Scripts\python.exe'
try {
    & $Monitor --headless --no-config --target '.\torture\process_tree.bat' --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id batch-tree --report-dir (Join-Path $GuideRoot 'batch-reports') -- --duration 30 --depth 1 --fanout 2 --memory-mb 2
    $BatchExit = $LASTEXITCODE
} finally {
    if ($null -eq $PreviousPython) {
        Remove-Item Env:\DRAINTRACER_PYTHON -ErrorAction SilentlyContinue
    } else {
        $env:DRAINTRACER_PYTHON = $PreviousPython
    }
}
```

The root is `cmd.exe`, then a Python worker starts two children. Depth `1`,
fan-out `2`, and 2 MiB per logical worker bound the example to three Python
workers and 6 MiB of fixture allocation, plus interpreter and launcher overhead.
Windows environment redirectors can add another process per worker: the checked
run saw seven processes total. Do not interpret an extra interpreter redirector
as an application restart. Child discovery can lag launch, so inspect the tree
and lifecycle evidence rather than expecting every child in the first sample.

Arguments are separate literal CLI tokens after `--`; the target's `--duration`
does not set the monitor timeout. Quoted paths and PowerShell variables remain
single arguments. DrainTracer protects its Windows batch launch boundary from
shell expansion, but a batch file's own commands can interpret input; use only
trusted scripts and do not pass production secrets in examples.

Expect judged coverage and no sustained-growth signal, exit `0`, and matching
reports under `$GuideRoot\batch-reports`. Ordinary timeout or Ctrl+C cleans up
the launched `cmd.exe`/Python tree. A partial-cleanup warning needs investigation;
the original fixture has a 30-second self-limit as a second bound. Unrelated
processes are not part of this launched tree.

Normal CLI launches close target stdin and redirect stdout/stderr to the null
device; **these recipes capture no target logs**. Target input cannot consume
dashboard controls. Explicit bounded capture, when needed, belongs to the
separate opt-in [workload recipe command](workloads.md).

Checked on 2026-10-31 with Windows build 26300, Python 3.14.7, .NET Framework's
committed fixture, and a source-installed v5.4.0 candidate. The native/batch runs
collected 64/65 samples, observed one/seven processes, judged windows with full
coverage, had no resource signals, and both returned `0`. Creation-time checks
confirmed all eight observed process identities had exited after ordinary
shutdown. Exact counters and process counts depend on interpreter/runtime setup.
