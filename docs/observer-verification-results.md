# Native observer verification and soak results

The complete fixed-budget verification ran on native Windows and WSL Linux:
nine repeated 65-second tree measurements followed by a **31-process,
30-minute native soak** on each platform. Both sets finished with
`state: "budget_failures"`. Preserve that result: the original local timing
ceilings were exceeded and have not been widened or relabeled as passing.
All recorded functional sampling, bounded-storage, reader, identity and owned
cleanup quality checks passed. Windows also produced an unexpected thread
signal in the nominally stable fixture, so its soak is not a clean leak-policy
result.

The complete [Windows record](measurements/observer-verify-windows.json) and
[Linux record](measurements/observer-verify-linux.json) retain every case and
breach. The [reference measurements and frozen budgets](observer-reference-results.md)
preceded verification under the [protocol](observer-protocol.md). No case was
selectively rerun, no ceiling was changed after seeing a result, and no
detector threshold was changed.

## Source and machine conditions

Both reference and verification used the same frozen benchmark/runtime source
`3f42974068ba5d9749abd40c8f6aeb07dc8b2be7`, candidate `v5.4.0`, with clean
tracked source and matching byte hashes. Verification ran from approximately
13:39 to 14:19 UTC on 2026-10-31. Native Windows 11 build 26300 used Python
3.14.7; WSL2 kernel `6.18.33.2-microsoft-standard-WSL2` used Python 3.14.4.
Both used psutil 7.2.2, Rich 15.0.0 and 16 logical CPUs.

Initial Windows available memory was 4,581,650,432 bytes with 79.4% host CPU,
compared with 26.2% CPU at reference start. WSL reported 7,227,195,392 guest
available bytes and 0.0% initial guest CPU. The platforms shared a developer
machine with worker-policy measurements, tests, builds and other development.
These observations establish loaded-machine conditions, not a causal proof
that each timing breach was caused by a particular concurrent task.

Later orchestration-only changes make interruption sequencing and long-phase
checkpoint labels clearer. They were not substituted into this fixed run.
The broadened Windows identity test/oracle correction at `1697360` changes
tests/docs only; production identity source in the measurement is unchanged.
Future runtime/benchmark changes require their own source-specific evidence;
these records must not be relabeled as a different release's measurement.

## Every frozen-ceiling breach

Windows exceeded ceilings in two of ten verification cases:

- One-process repetition 2: maximum collection **147.3523 ms** versus
  **144.4972 ms**, 1.98% over; handle-classification P95 **65.2894 ms** versus
  **58.5427 ms**, 11.52% over; classification maximum **81.597 ms** versus
  **78.99 ms**, 3.30% over.
- The 31-process soak: classification maximum **117.3903 ms** versus
  **108.1634 ms**, 8.53% over. Its classification P95 was 40.9421 ms,
  below the 74.4605 ms ceiling.

Linux exceeded ceilings in three of ten cases:

- One-process repetition 1: classification maximum **2.0257 ms** versus
  **0.4684 ms**, 332.47% over (about 4.32 times the ceiling). Its classification
  P95 was 0.1444 ms, below 0.2085 ms. This is a large relative outlier at a
  small absolute duration; it still fails the declared maximum.
- One-process repetition 2: maximum collection **8.1193 ms** versus
  **7.7756 ms**, 4.42% over.
- The 31-process soak: classification maximum **9.1734 ms** versus
  **8.5092 ms**, 7.81% over. Its classification P95 was 1.6051 ms,
  below the 4.3827 ms ceiling.

Every other declared metric/quantile ceiling passed in these cases, including
collection P95, mean/P95 sampler CPU, post-collection age estimates, whole
harness RSS and export duration. Means and P95s do not excuse maximum failures;
likewise one maximum does not describe the ordinary per-sample cost. The
harness returns failure for its `budget_failures` state. These short-reference
margins are not reliable universal performance guarantees. Reproduce on the
deployment's own machine and workload before adopting a budget; a future
amendment needs an explicit rationale and new complete verification evidence.

## Thirty-minute native observations

Both targets had independent 1,830-second lifetimes and were stopped by their
1,800-second monitor deadline. Each discovered all 31 logical processes and
independently confirmed all 31 observed identities had exited after owned
cleanup; zero cleanup identities remained unconfirmed.

- **Windows:** 3,600 full-coverage observations; mean achieved interval
  500.0 ms, maximum 544.1 ms. Collection mean/P95/max: 31.3893 / 63.4359 /
  177.8541 ms. Mean/P95 sampler CPU: 8.3342 / 14.6456% of one logical CPU.
  Whole-harness peak RSS 232,468,480 bytes; export 3,016.6726 ms. JSON was
  10,292,478 bytes, retaining 451 chart points at stride 8, and passed the
  bounded public reader. Headless resource-policy exit was `1` from threads.
- **Linux:** 3,601 full-coverage observations; mean achieved interval
  500.0 ms, maximum 502.8 ms. Collection mean/P95/max: 25.5410 / 38.2508 /
  80.3262 ms. Mean/P95 sampler CPU: 7.3380 / 10.1691%. Whole-harness peak
  RSS 267,464,704 bytes; export 1,743.1146 ms. JSON was 9,586,796 bytes,
  451 points at stride 8, and passed the reader. No resource warning;
  headless resource-policy exit `0`.

There were **zero failed samples, queue drops, paused observations, clock
adjustments, sampling gaps, monitor errors, export failures or incomplete
owned cleanup** in either soak or any of the eighteen short verification
runs. Fresh windows filled and exports remained bounded/readable. These
quality facts are separate from timing-ceiling failures and resource signals.

## Unexpected Windows thread growth

The fixture intentionally creates one waiting Python worker thread per
process, but that does not guarantee a constant native OS thread count. The
Windows soak recorded two thread-signal episodes, worst CRITICAL. First
warning was +300.5456 s and first HIGH +302.5455 s; memory and handles had
no growth signal. The Linux soak had no resource warning.

The retained Windows report shows real per-process counter transitions:
at approximately +288 s all 31 processes had three threads (total 93); at
+304 s eight had five and 23 had four (total 132), with a HIGH thread signal.
By approximately +1,788 s all had three again. Whole-run peak 186 also
includes startup: a retained +8 s sample had six per process. The fixture's
Python source does not itself create those additional threads after startup.
Their native origin was not traced, so attributing them to a particular
Windows component, security product, runtime mechanism, or monitor action
would be speculation.

This is an unexpected warning in a bounded, nominally stable exercise, not
proof of a leak or a quantified false-positive probability. It demonstrates
why a 75-second calibration cannot substitute for a long soak and why HIGH
policy outcomes must stay visible alongside performance diagnostics. The
transient increase is not removed from the data or used to silently tune
thresholds.

## Failure handling and limits

The separately recorded [native failure drills](observer-stress-results.md)
cover a surviving child after parent exit, bounded access loss/restoration,
collector stalls, wall-clock steps, slow consumers, injected disk exhaustion,
interrupted exports, explicit attached pause followed by ordinary resumed
detach, and deterministic stale/unknown-identity controls. Their source and
initial failed harness attempt remain explicit.

Physical power-management sleep/wake, forced kernel PID reuse, isolated
production throughput, arbitrary larger trees, protected service accounts,
native Linux hosts and human pilots are not established here. Private raw
reports stay outside version control; published aggregates contain no process
IDs, hostnames, user paths, credentials or target output. The documented
discovery and abrupt-crash policy remains a real boundary, not a promise that
normal cleanup executes after an OS crash or forced monitor termination.
