# Measured host-context component budgets

The scoped host refresh and cached-call criterion passed in all three
verification replicates on native Windows and Ubuntu under WSL. This is a
component cost result on the recorded shared machine. It does **not** turn
the broader T5 observer verification into a pass: that separate exercise
retains both `budget_failures` outcomes, seven timing-ceiling breaches and
the unexpected Windows thread signal.

The [protocol](host-budget-protocol.md) was committed at `48ccdc1` before
collection. Complete reference sets and derived limits were committed at
`1408a41` before either verification set ran. Both phases used the same
immutable source `68442ce1d1a2a294ff6d6d9c9120aa0154971441`, with matching
raw checkout SHA-256 bytes and canonical runtime version `v5.4.0`. This is
historical candidate-source evidence, not a claim that the published v5.4.0
artifact or a later release was measured under identical full source.

## Conditions and complete records

Native Windows 11 build 26300 used Python 3.14.7; WSL2 kernel
`6.18.33.2-microsoft-standard-WSL2` used Python 3.14.4. Both had psutil 7.2.2,
Rich 15.0.0 and 16 logical CPUs. Windows reference/verification started with
51.0%/48.1% host CPU and approximately 2.98/3.68 GB available memory; WSL
reported 0.3%/0.0% guest CPU and approximately 7.22/7.19 GB available. Guest
capacity is not independent host capacity. Collection ran serially on the
shared developer machine at approximately 15:05–15:06 UTC on 2026-10-31.

- Windows: [reference](measurements/host-reference-windows.json),
  [frozen budget](measurements/host-reference-windows.budgets.json),
  [verification](measurements/host-verify-windows.json).
- WSL Linux: [reference](measurements/host-reference-linux.json),
  [frozen budget](measurements/host-reference-linux.budgets.json),
  [verification](measurements/host-verify-linux.json).

Each platform has three reference and three verification replicates. Every
replicate retains all 200 refresh and 200 immediate cached-call timings,
nearest-rank P99, median and maximum. Every replicate had 200 valid memory
readings, 199 known CPU readings after the expected first priming observation,
and 200 matching cached observations. No target or detector background
thread was started. The source/environment/derivation and frozen budget
hash checks passed. No verification replicate was selectively retried and
no ceiling was amended after verification.

## Frozen ceilings and observed verification

The limits are twice the largest reference replicate statistic, subject to
the protocol's unchanged absolute floors. Values below list **P99 / maximum**
in milliseconds; observed figures are the largest across all three later
verification replicates, not a pooled or universal percentile.

- Windows refresh: ceiling **5.5678 / 6.1188 ms**; observed
  **0.9230 / 1.5020 ms**. Cached: ceiling **0.1000 / 0.5000 ms**; observed
  **0.0138 / 0.0212 ms**.
- WSL refresh: ceiling **1.0000 / 2.0000 ms**; observed
  **0.223574 / 0.378188 ms**. Cached: ceiling **0.1000 / 0.646970 ms**;
  observed **0.038803 / 0.066076 ms**.

Every P99 and maximum check passed. At the runtime's five-second refresh
cadence, the largest frozen refresh P99 ceiling is 0.111356% of that interval
and the largest refresh maximum ceiling is 0.122376%. These are component
wall-time allowances, **not** measured total CPU usage or target-throughput
overhead. Cached calls run at collection cadence and have their own limits.
The existing `benchmark_host.py` command, 200-call default and displayed
rounded one-millisecond-minimum P99 budget remain unchanged.

The first Windows reference at source `e465c0f` was faster (largest replicate
refresh P99 0.2756 ms), but its companion Linux phase failed before sampling
because Linux Git could not read a Windows-created worktree pointer. Both
the complete [initial Windows record](measurements/host-reference-attempt1-windows.json)
and [unused derived budget](measurements/host-reference-attempt1-windows.budgets.json),
plus the [zero-run Linux setup failure](measurements/host-reference-attempt1-linux.json),
remain retained. After the committed owner-Git provenance correction, both
complete reference sets were recollected from the new source before any
verification. The fresh Windows reference's largest P99 was 2.7839 ms and
maximum was 3.0594 ms; those observations determine its frozen ceilings.
No causal explanation for that variability is established by these records.

## What the timing includes

The harness times the complete `_sample_host()` call, including the host OS
queries, Python wrapper and published-age copy. Production target
`diagnostics.collection_ms` stops before `_diagnostics()` calls the host
sampler, so it is not evidence for this extra cost. The full observer's CPU
and RSS budgets passed separately with host sampling included, while its
collection/classification timing failures remain unchanged.

Forced refreshes stress the read path faster than its ordinary five-second
cadence; these rapid short repetitions do not establish long-term latency,
hardware-independent ceilings, precise scheduling bounds, physical sleep/wake
behavior, native Linux performance or a production SLA. Published host values
remain context rather than attribution to the target. Initial/unreadable CPU
readings stay unknown, and cache age remains tied to monotonic diagnostics.

## Reproduce without changing the frozen source

Use the recorded source in a clean immutable checkout with its runtime
dependencies. Choose new output filenames. Keep a separate results worktree
for committing reference/budget evidence before verification so the measured
checkout's source commit and byte hashes do not change.

```powershell
python torture/verify_host_budget.py reference --output torture/reports/host-reference-windows.json
# Preserve and commit the complete reference and its .budgets.json first.
python torture/verify_host_budget.py verify --budgets torture/reports/host-reference-windows.budgets.json --output torture/reports/host-verify-windows.json
```

Use Linux Python and distinct `-linux` filenames under WSL. For a
Windows-created worktree, provenance uses its owner `git.exe` through WSL;
the actual host measurements still use Linux Python/psutil. Other checkouts
use their ordinary Git. Verification refuses changed source/environment or
derived limits. A future breach returns failure and must remain visible.
