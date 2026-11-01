# Install, measure, change, and compare

This Windows PowerShell walkthrough uses disposable source-checkout fixtures.
It measures known memory growth, substitutes a steady implementation, and
compares the reports. It demonstrates resource evidence; it cannot prove an
allocation leak or establish that a real application fix is correct.

Allow about two minutes, Python 3.10+, roughly 160 MiB for the growth target,
and permission to launch your own processes. Use a normal terminal; these
fixtures need no administrator rights or production data. Start in a complete
DrainTracer checkout. The `torture` scripts are **not** installed by PyPI.

## Install a separate monitoring environment

```powershell
$GuideRoot = Join-Path $env:TEMP ('DrainTracerGuide-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $GuideRoot | Out-Null
$MonitorEnv = Join-Path $GuideRoot 'monitor-env'
python -m venv $MonitorEnv
& (Join-Path $MonitorEnv 'Scripts\python.exe') -m pip install .
$Monitor = Join-Path $MonitorEnv 'Scripts\draintracer.exe'
& $Monitor --version
& $Monitor --help
```

`pip install .` installs this checkout's candidate version. To monitor an
application without these source-only examples, installing `draintracer`
from PyPI is sufficient. Calling the environment's executables directly
avoids changing PowerShell's activation policy. The commands below use the
installed CLI, with fixture files copied from the checkout.

## Measure bounded growth

```powershell
$RunRoot = Join-Path $GuideRoot 'walkthrough'
New-Item -ItemType Directory -Path $RunRoot | Out-Null
$Worker = Join-Path $RunRoot 'worker.py'
Copy-Item -LiteralPath '.\torture\memory_leak.py' -Destination $Worker
& $Monitor --headless --no-config --target $Worker --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id walkthrough --build before --report-dir (Join-Path $RunRoot 'before') -- --duration 30
$BeforeExit = $LASTEXITCODE
```

The target allocates 2 MiB every 250 ms, touches the pages, and caps retained
allocation at 128 MiB. The monitor stops it after 16 seconds, before its
30-second self-exit. Startup growth during the first two seconds is excluded
from leak judgments, but remains in whole-run peaks.

The 10-second window needs at least 8.5 seconds of valid post-warm-up samples.
Expect `Memory leak signal HIGH` or stronger and exit `1` when the signal
reaches the default HIGH failure policy. A busy machine or unreadable counters
can change timing or prevent a judgment; inspect the report rather than
changing the expected result to a pass. No single sample establishes a leak.

## Export and interpret the evidence

Headless shutdown automatically writes matching JSON/HTML files under the
requested report directory. The printed paths include the target filename,
date, and a unique report ID. Read both paths from the summary, or select this
fresh run's sole JSON file:

```powershell
$BeforeReport = (Get-ChildItem -LiteralPath (Join-Path $RunRoot 'before') -Recurse -Filter *.json).FullName
$Before = Get-Content -LiteralPath $BeforeReport -Raw | ConvertFrom-Json
$Before.detection_ran
$Before.coverage.partial
$Before.session_facts.signals.memory
$Before.session_facts.peaks.private_bytes
$Before.outcome.target
```

`detection_ran: true` establishes that a window was judged. Check that coverage
is not partial and inspect unavailable metrics; unreadable counters are
unknown, and partial totals are lower bounds. `private_bytes` uses bytes;
divide by `1MB` in PowerShell for MiB. RSS is a working-set measurement and
can differ from private committed memory. Handles and threads are counts.
`session_facts` retains whole-session peaks and signals even when chart points
are thinned. A high peak alone is different from sustained growth.

Open the matching `.html` locally to inspect charts and contributors. Both
formats can disclose paths, process identities, timestamps, and labels; review
[privacy guidance](privacy.md) before sharing. This guide prints no real
credentials or service identities.

## Make the synthetic change and compare

Here the deliberate change is replacing a growing program with the committed
steady fixture at the **same copied path**. A real investigation would apply
and review an application fix instead. Keep target arguments, timing, settings,
machine conditions, and workload labels equivalent; `--build` identifies the
changed implementation.

```powershell
Copy-Item -LiteralPath '.\torture\steady.py' -Destination $Worker -Force
& $Monitor --headless --no-config --target $Worker --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id walkthrough --build after --report-dir (Join-Path $RunRoot 'after') -- --duration 30
$AfterExit = $LASTEXITCODE
$AfterReport = (Get-ChildItem -LiteralPath (Join-Path $RunRoot 'after') -Recurse -Filter *.json).FullName
& $Monitor diff $BeforeReport $AfterReport --strict --json
$ComparisonExit = $LASTEXITCODE
```

The steady fixture holds 8 MiB of its own allocation, four files, and two
workers. Expect a judged run without sustained leak signals and exit `0`.
That is evidence for this bounded workload and duration, not leak-free proof.

In the comparison, inspect `resources.memory.signal: "resolved"` and a lower
private-byte drift. The overall verdict can be `improved` or `mixed`: a small
handle/other drift change can coexist with the resolved memory signal. Read
every resource and caveat. `--strict` withholds equivalence when provenance or
coverage blocks comparison; it does not make every metric improve. Diff exit
`0` means comparison completed unless an explicit regression/budget policy was
requested. See [report comparisons](reports.md) for those gates.

## Recognize an unjudged run

```powershell
& $Monitor --headless --no-config --target $Worker --timeout 2s --warmup 0s --interval 250ms --window 10s --fail-on-inconclusive --report-dir (Join-Path $RunRoot 'short') -- --duration 30
$ShortExit = $LASTEXITCODE
```

This intentionally ends before the window fills. Expect `INCONCLUSIVE`,
collecting evidence rather than clear signals, and exit `3`. Without the
opt-in inconclusive policy, a short unjudged run can exit `0`; always read the
measurement result. Extend the duration instead of interpreting missing
signals as a clean leak check.

## Cleanup and checked results

Ordinary timeout or Ctrl+C stops the launched fixture tree and preserves
reports. Inspect target/cleanup outcome warnings if shutdown is partial.
The original fixtures remain unchanged in the checkout. Keep the temporary
folder while reviewing evidence; remove that explicitly named folder yourself
when finished. No already-running application is attached by this walkthrough.

Command checks on 2026-10-31 used Windows build 26300, Python 3.14.7, a clean
environment installed from a v5.4.0 source candidate, default thresholds, and
the flags above. The growth and steady runs each collected 64 samples, judged
windows, and reported full coverage. Growth reached HIGH (exit `1`); steady
had no signal (exit `0`). The strict diff was comparable, memory resolved,
overall mixed because of approximately 0.011 handles/second of fitted drift.
The short run was inconclusive (exit `3`). Exact resource values and timing
are machine-dependent. This is technical command validation; first-time
contributor usability acceptance remains tracked in
[issue #31](https://github.com/JunaidAhamed-7777/DrainTracer/issues/31).
