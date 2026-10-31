# Preregistered host-context cost verification

This protocol is committed before collecting new host-context timings for
issue #27. It measures the complete synchronous `_sample_host()` call,
including its Python wrapper and age-copy work, separately from the target
collection timer. `_diagnostics()` samples host context after the target's
`collection_ms` timer has stopped. Do not use that timer as a host-cost bound.

## Fixed sequence and scope

Use `torture/verify_host_budget.py` from one immutable clean source checkout
on native Windows and Ubuntu under WSL. Record source commit and SHA-256
bytes, OS/kernel, Python/dependency versions, logical CPUs, memory and initial
CPU pressure. These are two environments on one shared developer machine,
not independent hardware or a native-Linux claim. No hostname, PID, user path,
host measurement value history or target output belongs in public records.

Each platform runs **three reference replicates**, freezes one budget from
all three, then runs **three fresh verification replicates** against it.
Each replicate creates a new unstarted detector and makes **200 forced
refresh calls**, each immediately followed by **one cached call**. This is
the existing `benchmark_host.py` sequence; that command and its defaults
remain unchanged. Force refresh by clearing only `_host_sampled_at`, without
sleeping five seconds or starting the detector background thread. Measure
each call with `perf_counter_ns`. Preserve all 200 timings per path, their
nearest-rank P99 (`ceil(.99*N)-1`), median and maximum. No discarded warm-up,
selective retry, calibrated subtraction or threshold change is allowed.

The first CPU reading in a new detector is expected to be unknown while
priming psutil. Require all memory readings valid, CPU available on the
remaining 199 refreshes, and all cached readings to match their corresponding
refresh's timestamp/CPU/memory fields. An unexpected missing reading,
exception, source change or incomplete replicate fails quality and cannot
produce an accepted budget. Save progress and failures rather than inventing
timings. These calls read whole-host context; they launch/control no target.

## Frozen engineering ceilings

Derive each ceiling from the **largest replicate statistic** in the complete
reference set, using the following declared margins and absolute floors:

- Refresh P99: `max(1.0 ms, 2 * largest reference P99)`.
- Refresh maximum: `max(2.0 ms, 2 * largest reference maximum)`.
- Cached P99: `max(0.1 ms, 2 * largest reference P99)`.
- Cached maximum: `max(0.5 ms, 2 * largest reference maximum)`.

The refresh P99 floor preserves the existing benchmark's one-millisecond
minimum. The maximum floor allows an additional millisecond for rare local
scheduling interruptions; cached floors are one tenth and one half of that
minimum. These are explicit small engineering allowances, not statistical
confidence bounds. They do not replace the existing benchmark's displayed
rounded local budget. At the five-second runtime refresh cadence, a 1 ms
refresh P99 allowance is 0.02% of that interval. Report actual ceilings and
interval shares, rather than treating these floors as observations.

Commit the complete reference records and derived budget files **before**
verification. Verification must match platform, interpreter/dependencies and
all recorded source bytes; it cannot overwrite a prior result. Every P99 and
maximum must meet its frozen ceiling in all three verification replicates.
A breach returns failure, retains its magnitude and records the criterion as
unmet. Do not widen or rerun until green. Any later amendment needs a new
protocol and complete independently identified reference/verification set.

## Meaning and boundaries

Passing proves only this component's repeated refresh/cached call-cost
criterion on the recorded environment. It does not establish universal
percentiles, target-throughput overhead, production availability, a human
pilot or an end-to-end observer budget. Forced refreshes intentionally stress
the read path faster than the real five-second cadence. Thread CPU priming,
unavailable counters and monotonic published age keep their runtime semantics.

The larger T5/#26 reference and soak records remain separate historical
evidence: both full verification sets had `budget_failures`, with seven
timing-ceiling breaches and an unexpected Windows thread signal. Their CPU
and RSS ceilings passed, but this component exercise cannot turn those
failed collection/classification ceilings into passes or relabel source
`3f42974` as a later release's measurement.

## Preserved provenance-harness amendment before verification

The first reference attempt used source `e465c0f`. Its complete Windows
reference and derived budget are retained as `host-reference-attempt1-windows`
records. Linux failed before any timing replicate because Linux Git cannot
read the Windows-created checkout's absolute `C:/...` worktree pointer.
Its `CalledProcessError`, zero runs and active preparation state are retained
in `host-reference-attempt1-linux.json`. No verification used that budget.

The amendment selects the checkout owner's `git.exe` under WSL and translates
only the Git metadata directory using `wslpath`, as the existing calibration
harness does. Linux Python/psutil still perform the actual Linux measurements.
Record the runtime version alongside the source hashes. After committing this
provenance-only fix, recollect both complete reference sets from one new
immutable source, commit their derived budgets, then verify once per platform.
The timing sequence, margins, floors and quality rules above do not change.
