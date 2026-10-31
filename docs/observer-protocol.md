# Observer cost and lifecycle stress protocol

This protocol precedes issue #26 performance collection. It follows the
[calibration protocol](calibration-protocol.md), retains the default detector
thresholds, and scopes budgets to the recorded source, OS, Python, machine
conditions, and at most 31 logical fixture processes. It does not establish
hardware-independent cost limits or the performance of every Windows service.

## Pilot, budgets, and verification

1. Measure native stable process trees of depth 0/2/4 and fan-out 2: one,
   seven, and 31 logical Python processes, each holding 1 MiB and one waiting
   worker. Use the base Python executable. Run each size for 65 seconds,
   warm up for three seconds, sample every 500 ms, and keep default
   10/30/60-second windows. Repeat the complete size sequence three times.
   Each target has a separate 90-second self-limit.
2. Record per-sample collection time and monitor CPU, handle-classification
   cost/age, achieved cadence, failed/dropped samples, and post-collection
   snapshot-age estimates. Measure whole harness-process RSS every 50 ms,
   including cleanup and export, and time export separately. The watcher and
   harness are included in observer cost; this is not an application-throughput
   A/B study. Age estimates use wall time minus recorded collection time and
   are not precise transport latency.
3. Exercise maximum retained shape separately by replaying fresh copies of
   a real captured 31-process snapshot until the unmodified history holds
   `SNAPSHOT_LIMIT - 1` entries (7,199 currently). This is a **synthetic
   retention/export stress**, not a one-hour native run or calibration sample.
   Copy nested dataclasses and numeric scalars to avoid sharing all retained
   objects. Measure RSS and export cost, validate the resulting JSON through
   the bounded public reader, and verify exact facts and first/last points
   survive any contract-driven export thinning.
4. Derive per-size budgets from the maximum pilot run values. P95 collection,
   mean/P95 sampler CPU, and P95 post-collection age receive a 1.5 multiplier;
   observed maxima and export duration receive a 2.0 multiplier. The RSS
   ceiling is 1.25 times the largest measured native or full-retention peak.
   These multipliers are declared engineering margins applied to measurements,
   not confidence intervals. Keep unknowns unknown; do not fabricate a
   classification-cost limit when classification was unavailable.
5. Freeze the resulting budget file before verification. Repeat the three
   native sizes three times with identical settings, then run one 31-process
   **30-minute native soak** at the same cadence and windows. Its independent
   target self-limit is 1,830 seconds. Verify applicable budgets, zero failed
   samples/queue drops/temporal resets, matching readable exports, and complete
   owned cleanup. Preserve all failures rather than rerunning until green.
   An amended budget requires a documented reason and a fresh complete
   verification set.

Run Windows and WSL independently, using distinct artifacts. They can share
the host concurrently with calibration and other development tasks; record
that condition, baseline CPU/available memory, versions, and exact source
hashes. These are loaded developer-machine measurements, not isolated-runner
benchmarks. Refuse memory-heavy retention stress when less than 2 GiB is
available to the environment at its start. Publish aggregate results only,
without raw reports, process IDs, hostnames, credentials, or user paths.

## Lifecycle and injected failure cases

- **PID reuse:** run the identity-change and retired-PID regression cases;
  deterministic fake identities are explicitly labelled. Real kernel PID
  reuse is not forced and never authorizes a blind control.
- **Short-lived parent:** run the native bounded `parent_exits.py` tree and
  verify a surviving owned child is discovered and cleaned.
- **Access loss:** deny the sampler's owned-root memory read for a bounded
  interval, then restore it. Record unavailable/partial evidence and fresh
  detection windows; the other process and identity checks stay real.
- **Sleep/resume and clocks:** inject a bounded collector stall plus forward
  and backward wall-clock steps while monotonic time remains the control
  clock. Verify temporal-reset counters and window restarts. Do not suspend
  the shared development computer, and do not claim physical power-management
  or every hardware wake-up path was tested.
- **Slow consumer:** use a four-item consumer queue, stall polling while the
  real sampler continues, then verify reported drops, exact producer-owned
  facts, and bounded queue/detail retention.
- **Disk exhaustion:** inject `ENOSPC` at the export boundary without filling
  the machine's disk. Verify explicit failure, no partial temporary artifact,
  retained in-memory evidence, and completed owned cleanup.
- **Shutdown during export:** inject Ctrl+C during HTML generation after JSON
  was written. Verify corrected same-ID JSON, interruption status, and no
  owned process left running. Also cover interruption before JSON exists.
- **Attached pause/ordinary shutdown:** launch a disposable external target,
  verify its creation time, attach/pause it explicitly, then shut the monitor
  down normally. Confirm that same identity is alive and resumed; only the
  fixture owner cleans it up afterward.

The fixture lifetimes, queue/history limits, and API bounds are part of the
evidence. Document discovery limits and the unexpected-crash policy separately:
ordinary cleanup is tested, while a forcibly killed process cannot run Python
`finally` blocks. No recovery test may resume/kill a PID without its proven
identity and ownership.
