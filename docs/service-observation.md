# Observe an already-running Windows service

Use a disposable service that you own, or an application service whose owner
has authorized observation. Never rehearse against a system-critical service.
Ordinary `--pid` timeout/quit detaches; it does not stop an attached service.
In the dashboard, **Space pauses/resumes and K terminates the target**. Avoid
those controls in an observation session; the headless recipe below has no
interactive target-control keys.

## Disposable service validation

The source checkout includes `torture/exe/DisposableService.cs` and a bounded
verification script. On a disposable Windows machine, from an **elevated
PowerShell 7** terminal in the checkout root, with runtime dependencies
installed, run:

```powershell
pwsh -NoProfile -File .\torture\verify_service_recipe.ps1
```

This explicit test creates a fresh `DrainTracerRecipe_<random hex>` service
under the built-in **LocalService** account, using the .NET Framework C#
compiler. It needs no service password. Only its new temporary executable
folder receives LocalService read/execute permission. It refuses an existing
service name and verifies the registered binary path before any service
control or deletion.

The fixture holds 8 MiB, starts two bounded workers, and accepts one test
exercise command that replaces a single 1 MiB block. Its independent lifetime
is 120 seconds. The script observes it for 16 seconds with a two-second warm-up,
250 ms sampling, and a 10-second window; it then verifies the **same PID and
creation time** are still running after ordinary detach. Only afterward does
the fixture's owner stop/delete its own test registration. The script verifies
registration removal and retains its explicitly named temporary folder for
inspection. It does not install a persistent monitoring service or configure
automatic startup.

The dedicated `Verify disposable service observation` CI workflow runs this
same script on an elevated, disposable `windows-2025` runner with Python 3.10.
Its synthetic summary records OS/Python versions, valid coverage, sample count,
detach survival, and registration cleanup, without real service credentials
or machine names. A successful compilation alone is not an SCM/detach test;
the PR must have a successful service-observation job before this validation
is considered complete.

The CI job also injects a lost acknowledgement immediately after creation.
Cleanup records creation intent before the SCM request and inspects only that
unique name/exact binary registration even when creation does not return
normally. The negative test passes only when registration removal completes
and the original simulated error is preserved.

## Identify and inspect the intended service

For a disposable service already started by its owner, substitute its exact
known name and expected executable path. The examples below use placeholders;
do not copy the name of a Windows system service.

```powershell
$ServiceName = 'DrainTracerRecipe_<your-generated-hex>'
$Service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
$Service | Select-Object Name, State, ProcessId, PathName, StartName
$ServicePid = [int]$Service.ProcessId
Get-Process -Id $ServicePid | Select-Object Id, ProcessName, StartTime, Path
```

Confirm that the service is Running, the PID is nonzero, and the path/account
match the disposable workload you intended. A shared-process service may share
one PID with other services; resource totals then describe that entire process,
not an individual service. Choose a dedicated disposable process for this test.
If the service exits/restarts or its PID/creation time changes, discard the
session and identify it again. Direct CLI PID attachment selects the process
current at attachment, so separate preflight queries cannot eliminate a PID
reuse race. This recipe observes only; it never authorizes a target control.

## Monitor, exercise, and detach

Using the installed `$Monitor` from the [first-result setup](first-result.md#install-a-separate-monitoring-environment):

```powershell
& $Monitor --headless --no-config --pid $ServicePid --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id disposable-service --report-dir (Join-Path $GuideRoot 'service-reports')
$ObservationExit = $LASTEXITCODE
```

Exercise your own non-production service through its normal request mechanism
while monitoring. The supplied test script sends its fixture's single bounded
exercise command before observation. Do not send custom controls, traffic, or
credentials to an unrelated service. Startup that happened before attachment
is not measured; the warm-up covers settling after observation begins.

The 10-second window needs about 8.5 seconds of valid post-warm-up evidence.
The bounded steady fixture should produce judged coverage and no sustained
leak signal. Exit `3` under this recipe means a window was not judged, not that
the service is clean. Permission denial can leave some counters unavailable or
totals partial while attachment succeeds; inspect measurement/coverage fields
and never interpret unknown values as zero. Elevate only when authorized and
necessary to read that service's counters. The fixture test already needs
elevation to create/delete its own registration; observation of an existing
same-user process need not always require it.

JSON and matching offline HTML are exported at ordinary timeout or Ctrl+C.
Private bytes measure Windows committed private memory, RSS is working set,
and handle/thread values are counts. A stable window or growth signal is
resource evidence, not allocation-cause proof. Review the original reports
before sharing; real service paths, accounts, and IDs can identify a machine
or workload even when no credentials were intentionally collected.

After ordinary detach, query the named service and process again. Verify the
service is still Running with the original PID and creation time. Inspect
`mode: "attach"`, `outcome.target.state: "left_running"`, coverage, and any
cleanup warnings in the report. An operator outside DrainTracer remains
responsible for stopping their own disposable service and removing its test
registration. The supplied verifier performs that cleanup itself, guarded by
its unique name and exact binary path.

Checked on 2026-10-31: local Windows build 26300/Python 3.14.7 compiled the
fixture, rejected invalid names, parsed the verifier, and refused service
creation from a non-elevated terminal. Actual SCM execution passed in
[CI run 41200531625](https://github.com/JunaidAhamed-7777/DrainTracer/actions/runs/41200531625)
at commit `332ffa3`: Windows build 26100/Python 3.10.11, 64 samples over the
16-second observation, judged windows and full coverage, the same LocalService
process running after detach, and registration removal confirmed. The separate
lost-acknowledgement cleanup drill also passed. The synthetic result artifact
is retained for three days; the job record and reproducible verifier remain
available. These checks cover a dedicated disposable service, not every
service account, protected process, or shared-host configuration.
