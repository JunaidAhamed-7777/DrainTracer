# New-worker growth policy evidence protocol

This protocol precedes issue #52 measurements. Retain the existing full
aggregate severity policy and existing attribution. The question is whether
representative controlled accumulation, growth within one process, and
bounded churn support distinguishing capacity growth in the explanation
without silently reducing its CI severity. This is a local reference exercise,
not human-pilot demand or proof of allocation causes.

## Fixed cases and inputs

Run these three cases in order, repeat the full order three times, and collect
native Windows and WSL Linux separately. Each monitor lasts 75 seconds at
500 ms intervals, with three-second warm-up, default 10/30/60-second windows,
and unchanged default thresholds/HIGH failure policy. Each target has a
90-second independent lifetime. Use base Python on Windows to avoid virtual
environment redirector processes.

1. **Accumulate workers:** start one worker every two seconds up to eight;
   each holds/touches 12 MiB and waits for the remaining target lifetime.
   Held payload increases to 96 MiB and then plateaus. Total private memory
   also includes each new interpreter's overhead; do not claim payload
   matching makes aggregate memory counters identical.
2. **Grow inside one process:** `memory_leak.py` holds another 12 MiB every
   two seconds, capped at 96 MiB. This matches explicit payload increments
   and cap with case 1, while changing where the growth occurs.
3. **Churn workers:** start a 12 MiB worker every two seconds, each surviving
   750 ms. The active-child cap is eight and total launches cap 64, with
   ordinary reaping and cleanup. Resource peaks can exceed steady state,
   but workers do not accumulate. Polling may miss short-lived workers.

Publish every run, including unexpected warnings or attribution misses.
Record exact inputs/source/environment/settings, coverage, worst resource
tier, first warning/HIGH delay, actual maximum tree size, and independent
owned cleanup checks. For memory alerts preserve aggregate severity and
new/existing positive-growth contributions, contributing-process counts,
and the fraction attributed to new processes. Do not persist raw identities,
names, paths, output, or environment values in aggregates.

## Analysis declared before collection

- Count intended memory warnings/HIGH detections for accumulating workers and
  within-process growth, plus bounded-churn warnings/HIGH occurrences.
- Require at least one memory alert with `mostly_new_processes` for every
  accumulation run and at least one with mostly existing growth for every
  within-process run before making those attribution claims. Preserve failures
  rather than changing case inputs or rerunning individual cases until green.
- Compare the explanation and CI exit effects. A process start is capacity,
  not proof of a leak; an unbounded count of individually healthy workers can
  still exhaust resources, so intent alone must not silently waive an aggregate
  failure. Workload warm-up, input metadata, and explicit policy remain visible.
- Reuse the delivered history/contributor machinery; do not introduce an
  alternative attribution implementation or new automatic severity reduction.
- Evaluate optional deeper tracing against needs demonstrated by this evidence.
  Record potential privilege, cost, privacy, and blind-spot boundaries; choose
  no adapter when local aggregate fixtures do not justify one. Do not claim
  a measured adapter cost or consent-based pilot result when no adapter/pilot
  was run. Human requirements discovery remains tracked by issue #49.

Windows and WSL may share the host with the observer verification/soak and
development. Record this condition and initial CPU/memory; these are not
isolated performance measurements. Keep resource limits and failed evidence.
Two-second smoke mode only checks orchestration and is not policy evidence.

From a clean source checkout with dependencies installed, collect each platform
using its own Python and a distinct new destination:

```powershell
python torture/worker_policy.py --output torture/reports/worker-policy-windows.json
```

```sh
python torture/worker_policy.py --output torture/reports/worker-policy-linux.json
```

Existing records are refused. Aggregate checkpoints are atomic, and Ctrl+C
stops the sequence after owned cleanup even when an earlier HIGH determines
the individual run's exit code.
