# Repeated calibration protocol

This protocol is committed before collecting the issue #24 measurement set.
It evaluates bounded synthetic resource patterns, not allocation causes,
production accuracy, or a probability that a result is correct. No default
threshold change is proposed by this work. Any future threshold proposal needs
both growth and non-growth results, including observed misses and warnings.

## Fixed design

- Run every scenario three times on native Windows and Linux under WSL.
- Use the unmodified default thresholds and windows of 10, 30, and 60 seconds,
  with 500 ms requested sampling. Each monitor run lasts 75 seconds so even
  the 60-second window can fill after a three-second warm-up.
- Use a three-second warm-up unless the named scenario explicitly tests the
  zero-warm-up condition. Whole-run peaks still include warm-up.
- Run each platform's cases sequentially, in scenario order, and repeat that
  order three times. Windows and WSL may run concurrently on their shared
  host; record baseline host pressure and this limitation rather than claiming
  isolated benchmark conditions. No case runs against production data.
- Every target has a 90-second independent lifetime. At most one target tree
  per platform is active. Use a base Python executable on Windows to avoid
  counting virtual-environment redirectors as application workers.
- Retain private per-run JSON/HTML evidence under the explicitly chosen output
  directory. Publish only aggregate, identity-free measurement records and
  documentation; do not commit raw leak reports or logs.
- Record the exact source commit and file hashes, OS/Python/dependency
  versions, logical CPUs, physical memory, baseline available memory/CPU,
  configured thresholds/windows, sample/coverage counts, failed/dropped
  samples, measured cadence/collection cost, first signal by resource and
  tier, maximum tree size, and cleanup outcome. Hostnames, user names,
  environment values, process IDs, and absolute user paths are not aggregate
  fields.

## Scenarios and resource limits

1. **Stable:** `steady.py`, 8 MiB held, four files, two workers, warm-up 3 s.
   No sustained-growth signal is intended after settling.
2. **Cache then plateau:** a dedicated fixture adds 1 MiB every 250 ms until
   24 MiB is held, then holds it; warm-up 0 s. This is intentionally bounded
   benign retention. Record warnings during the initial ramp rather than
   treating a later plateau as proof that no warning occurred.
3. **Startup spike:** `startup_spike.py`, 96 MiB over 2.5 s, then holds;
   warm-up 0 s. Record any warning as a known benign-startup warning.
4. **Startup with warm-up:** identical input, warm-up 3 s. Compare evidence
   with scenario 3 without claiming the warm-up removes arbitrary slow leaks.
5. **Allocation/recovery:** a dedicated fixture allocates/touches 32 MiB,
   holds one second, releases it, and waits one second, repeatedly. At most
   32 MiB of fixture allocation is retained; allocator RSS retention can
   differ from live allocation.
6. **Worker churn:** a dedicated fixture launches one short-lived child every
   500 ms, with child lifetimes 750 ms and a cap of four active children.
   The parent owns/reaps them and attempts cleanup on exit. Record discovery
   limits; a 500 ms sample cannot guarantee seeing every short-lived worker.
7. **Slow memory growth:** `memory_leak.py`, 0.25 MiB each 500 ms (0.5 MiB/s),
   maximum 48 MiB. This remains below the cap for the measured 75 s. Record
   the first warning and HIGH threshold delay separately; absence of HIGH is
   a known-policy miss for this workload, not a clean result.
8. **File-handle growth:** `handle_leak.py`, ten files each 250 ms, cap 800.
   Growth stops at the cap; distinguish an early warning from later plateau
   behavior. Linux counts file descriptors rather than Windows handles.
9. **Thread growth:** `thread_leak.py`, one blocked worker each 250 ms,
   cap 80. Growth likewise stops at the cap; it is not an infinite leak.
10. **Partial access:** a stable root and one stable child, 1 MiB each. Inject
    `psutil.AccessDenied` into memory reads for the owned root only; preserve
    normal child sampling. This is explicitly a deterministic permission
    fault injection, not a claim to reproduce every operating-system ACL.
    Unknown/partial memory must remain qualified rather than look clear/full.
11. **Larger tree:** `process_tree.py`, depth 3, fan-out 2, 1 MiB each: fifteen
    logical Python processes. Record actual maximum discovery and coverage,
    rather than assuming all children appear in the first sample.

## Analysis decided before the run

Report each scenario's repetitions, valid/partial/unavailable evidence,
observed warning resources/tiers and first-warning delays, and missed intended
growth. For benign scenarios count **runs with any warning** and runs with HIGH
or stronger; these counts describe this fixture set, not a false-positive
probability. For growth scenarios count the intended resource's warning and
HIGH detections and retain misses explicitly. Do not discard failed runs or
rerun selectively until expected results appear. Operational failures stay
in the dataset with their errors; a corrected harness requires a documented
amendment and a fresh complete set.

The harness aborts before another case if cleanup is incomplete or cannot be
confirmed. Ctrl+C retains the partial dataset and stops the sequence. Its
two-second `--smoke` mode validates orchestration only and is explicitly marked
as a harness smoke, never as a calibration measurement.

Run the committed harness from a clean checkout with dependencies installed:

```powershell
python torture/calibrate.py --output torture/reports/calibration-windows.json --concurrent-windows-wsl
```

In WSL/Linux, use that environment's Python and a distinct output name:

```sh
python torture/calibrate.py --output torture/reports/calibration-linux.json --concurrent-windows-wsl
```

Omit `--concurrent-windows-wsl` when the platforms run sequentially on separate
or otherwise quiet hosts. Existing datasets are refused rather than overwritten.
The output is checkpointed atomically after every run. Snapshot age at
consumption includes collection time and scheduling; it is not a pure queue
latency measurement. Each target's first warning is timed from monitor launch,
not from an independently instrumented allocation event.

Minimum useful durations must account for requested cadence, valid sample
coverage, warm-up, and the longest intended window. A 60-second window needs
roughly 51 seconds of valid evidence after warm-up; observed timing adds
scheduling/discovery costs. Recommend conservative workload-specific durations
from the measured delays, and distinguish an unjudged interval from judged
evidence without a signal. Keep calibration guidance scoped to the recorded
platforms, tree sizes, fixtures, and machine conditions.
