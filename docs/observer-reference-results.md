# Observer cost reference and frozen budgets

The pre-registered [protocol](observer-protocol.md) produced complete native
reference sets on Windows and WSL Linux at source
`3f42974068ba5d9749abd40c8f6aeb07dc8b2be7` (candidate `v5.4.0`). Each
platform ran one-, seven-, and 31-process stable trees for 65 seconds, repeated
three times, with three-second warm-up, 500 ms cadence, and default
10/30/60-second windows. No reference run had a warning, partial coverage,
failed sample, queue drop, temporal reset, monitor error, or incomplete owned
cleanup. Every report pair was written and JSON passed the public reader.

These are measured engineering reference values on a loaded shared developer
machine, not hardware-independent limits or application-throughput A/B
results. The harness RSS watcher and report reader are included in whole
harness footprint. No human pilot or native Linux host is represented.

## Preserved evidence

- [Windows reference](measurements/observer-reference-windows.json) and
  [frozen budgets](measurements/observer-reference-windows.budgets.json).
- [Linux reference](measurements/observer-reference-linux.json) and
  [frozen budgets](measurements/observer-reference-linux.budgets.json).

Collection began on 2026-10-31 at 13:25 UTC. Native Windows 11 build 26300 used
Python 3.14.7; WSL2 Linux kernel `6.18.33.2-microsoft-standard-WSL2` used
Python 3.14.4. Both used psutil 7.2.2, Rich 15.0.0, and 16 logical CPUs.
Windows had 16,477,028,352 physical bytes and 3,808,788,480 available at the
start, with 26.2% baseline host CPU. WSL reported 7,977,209,856 guest physical
bytes and 7,223,627,776 available, with 0.0% guest baseline CPU. Windows and
WSL measurements ran concurrently with development; guest availability is
not independent host capacity. Exact source byte hashes, inputs, repetitions,
settings and diagnostics remain in the aggregates.

## Reference values by tree size

Each figure is the **maximum across the three reference runs** of the named
per-run statistic. CPU uses psutil's percentage convention where one fully
occupied logical CPU is 100%, not percentage of all 16 CPUs combined.

- **One process:** Windows P95 collection 56.42 ms, mean sampler CPU 8.39%,
  P95 handle classification 39.03 ms, P95 post-collection age estimate
  96.31 ms. Linux: 3.55 ms, 1.46%, 0.139 ms, 94.47 ms respectively.
- **Seven processes:** Windows 59.49 ms, 8.26%, 39.56 ms, 94.92 ms;
  Linux 11.60 ms, 2.74%, 0.518 ms, 97.29 ms respectively.
- **31 processes:** Windows 71.55 ms, 9.91%, 49.64 ms, 95.92 ms;
  Linux 54.74 ms, 8.19%, 2.922 ms, 95.60 ms respectively.

Classification passes and ordinary samples have different populations; the
classification P95 must not be added to collection P95 as if they were
independent costs. Age is the wall-clock age observed at consumption minus
recorded collection time, clamped at zero. It includes polling/scheduling
and is an estimate, not precise queue transport latency.

## Full retention and export

The separate synthetic replay used fresh copies of a captured 31-process
frame, filling 7,199 retained snapshots with nested dataclasses and scalars
copied. This represents bounded retained shape, not a one-hour native run.

- Windows peak whole-harness RSS: **573,874,176 bytes**; fill 7,646.73 ms;
  export 4,954.06 ms; JSON 13,121,029 bytes.
- Linux peak whole-harness RSS: **569,974,784 bytes**; fill 4,941.45 ms;
  export 3,019.79 ms; JSON 12,318,420 bytes.

Both exports retained 451 chart points at stride 16, passed the bounded public
reader, and preserved exact peak/signal facts plus the first and last chart
points. Live in-memory history was not truncated to make export pass. The
50 ms RSS watcher can miss a shorter peak; its resolution and own overhead
are recorded. Export is a synchronous operation measured separately from
background collection.

## Budgets fixed before verification

The declared margins are 1.5 times maximum reference P95/mean values,
2.0 times reference maxima and export duration, and 1.25 times the largest
native or synthetic-retention RSS peak. They are engineering margins rather
than statistical confidence intervals. Every per-tree metric is in the
frozen JSON; representative P95 collection ceilings are:

- Windows one/seven/31 processes: **84.6278 / 89.2277 / 107.3268 ms**.
- Linux one/seven/31 processes: **5.3179 / 17.3943 / 82.1125 ms**.

Global whole-harness RSS ceilings are **717,342,720 bytes** on Windows and
**712,468,480 bytes** on Linux; export ceilings are **9,908.1226 ms** and
**6,039.575 ms**. These ceiling values were generated from the complete
reference data and frozen before any verification sequence. A budget file
does not itself prove the later sequence passed. Preserve any verification
breaches, and require a documented amendment plus a new complete verification
set before replacing a failed budget.

To reproduce, keep the measured runtime and benchmark source fixed, use a
new destination, then verify against the generated file:

```powershell
python torture/observer_benchmark.py reference --output torture/reports/observer-reference-windows.json
python torture/observer_benchmark.py verify --budgets torture/reports/observer-reference-windows.budgets.json --output torture/reports/observer-verify-windows.json
```

Use that environment's Python and distinct Linux filenames in WSL. Verification
repeats the nine native runs and then a 31-process, 30-minute native soak.
The harness rejects changed runtime/benchmark byte hashes or platform and
refuses overwriting records. Reference collection and budget derivation are
not final soak acceptance. Additional native identity tests remain part of
source review; the [failure-drill evidence](observer-stress-results.md) records
its distinct source and test boundaries.

The [complete verification and 30-minute soak results](observer-verification-results.md)
preserve all frozen-ceiling failures and the unexpected Windows thread signal.
The original budgets are unchanged; reference completion does not imply they
passed subsequent verification.
