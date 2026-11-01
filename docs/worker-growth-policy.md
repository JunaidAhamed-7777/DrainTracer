# New-worker growth keeps full aggregate severity

DrainTracer retains its existing policy: **aggregate growth keeps its full
severity when most positive growth comes from newly started workers**. The
message explains added capacity, and existing contributor/history records
identify the split. A new-worker label does not silently downgrade a warning
or waive CI failure. No detector thresholds or default severity policy change.

## Repeated evidence

The [protocol](worker-growth-protocol.md) was committed before collection.
[Windows](measurements/worker-policy-windows.json) and
[Linux](measurements/worker-policy-linux.json) aggregates preserve all nine
runs per platform: three cases, repeated in order three times. Source was
`ceb927a` (candidate `v5.4.0`), with complete source hashes in each file.
Both sets ran on 2026-10-31, approximately 13:42–13:53 UTC, concurrently with
observer validation and development on the shared machine.

Native Windows 11 build 26300 used Python 3.14.7; WSL2 kernel
`6.18.33.2-microsoft-standard-WSL2` used Python 3.14.4. Both used psutil 7.2.2,
Rich 15.0.0, 16 logical CPUs, 500 ms sampling, default 10/30/60-second windows,
three-second warm-up and 75-second monitoring. Targets had separate 90-second
lifetimes. Initial Windows available memory was 2,742,562,816 bytes with
12.7% host CPU; WSL guest availability was 6,980,788,224 bytes with 0.6% CPU.
Guest memory is not independent host capacity. Full environment and fixed
inputs are recorded in the aggregates.

- **Accumulate workers:** one child every two seconds, at most eight, each
  holding 12 MiB; payload grows to 96 MiB then plateaus. Every run discovered
  all nine processes. Memory reached CRITICAL in 3/3 Windows runs and HIGH
  in 3/3 Linux runs. The first HIGH appeared at 11.55–12.05 s on Windows and
  11.50–11.51 s on Linux. All recorded memory alerts attributed 100% of
  positive growth to new processes on Windows, and 99.74–100% on Linux.
  Windows also recorded handle HIGH and thread MEDIUM in 3/3; Linux recorded
  neither additional resource warning. Platform process overhead and handle/
  descriptor semantics differ; those results are not normalized away.
- **Grow inside one worker:** add 12 MiB every two seconds, capped at 96 MiB.
  Memory reached MEDIUM in 3/3 on each platform, first 12.06–12.09 s on
  Windows and 11.50–12.01 s on Linux. All memory-alert positive growth was
  attributed to existing processes. HIGH occurred in 0/3 on each, so the
  default HIGH headless policy returned zero despite known growth. No
  handle/thread warnings occurred. This explicit policy miss is preserved.
- **Bounded churn:** start a 12 MiB worker every two seconds with a 750 ms
  lifetime. No memory/handle/thread warning in 3/3 on either platform.
  At most two simultaneous processes were observed. Across each run the
  snapshots captured 38 Windows / 39 Linux identities including the root;
  those counts do not prove every short-lived child was discovered.

All 18 runs had full recorded coverage, no failed samples, queue drops,
temporal resets, monitor/export errors, or incomplete owned cleanup. Public
JSON-reader round trips passed. Every accumulation run contained a
`mostly_new_processes` alert, and every within-process run contained existing
growth attribution, as required before collection. Alert counts/episodes
varied across repeats; identical worst tiers do not establish a probability
of correctness or a continuously raised warning.

The explicit payload increments and cap match between the first two cases.
Aggregate counters do **not** match: each added interpreter has its own private
memory, handles and threads. Their different severities therefore cannot be
interpreted as a severity rule based on worker identity. Strict experiment
equivalence is not claimed across these different fixtures.

## Rationale and CI effects

An application can exhaust memory, handles or threads by accumulating
individually healthy workers. Treating every new process as harmless would
hide that resource risk. Conversely, deliberate scale-out can be expected
capacity rather than a leak inside a worker. Keep the aggregate signal and
make that distinction explicit in the explanation; counters do not determine
the application's intent or allocation cause.

Under the unchanged default HIGH policy, all six accumulation runs exited
`1`, while within-worker MEDIUM and churn runs exited `0`. An exit of zero
does not mean no growth was observed. Selecting MEDIUM would react to these
within-worker signals; it needs matched workload controls and an explicit
policy decision, not a global threshold reduction inferred from one fixture.
Use workload metadata, expected operation/worker counts, warm-up and resource
budgets to assess intended capacity. Approved baseline/suppression workflows
must retain their normal scope, review and expiry checks; the new-worker flag
alone does not authorize suppression.

Contributor shares and new/existing components describe **positive growth**.
Exits and decreases can offset those components in the net window change.
They do not form a net accounting identity, and retained lifetime first/last
rankings can differ from an alert-window capacity ranking.

## Tracing decision and boundaries

Keep the delivered process identity, history, lifecycle and contributor
machinery. The [tracing scope decision](deeper-tracing-scope.md) evaluates
cooperative Python and Windows native options, including privileges,
unmeasured adapter cost, privacy and missed history. No adapter is selected
without a demonstrated investigation need; none is enabled or represented
as measured by these aggregate runs. Any future tracer must be opt-in with
its own measured cost and explicit limitations.

This local fixture set supports the stated aggregate-severity policy and its
explanation. It does not fulfill the consent-based human pilot in issue #49,
establish production intent, prove universal churn tolerance, test every
permission boundary, or justify new platform/support commitments. Reproduce
with the protocol's commands and preserve unexpected outcomes rather than
rerunning individual cases until they fit the policy.
