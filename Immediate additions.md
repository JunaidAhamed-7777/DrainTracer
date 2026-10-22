# Immediate additions

> **Status, 2026-10-22.** This assessment is preserved as written. Since
> then, release v5.2.0 implements (see [RELEASES.md](RELEASES.md)):
> both P0 items (measurement validity, evidence states, and optional headless
> policies; private bytes as the named memory metric), P1 process
> attribution and lifecycle, P1 defensible comparisons (experiment
> provenance, qualified and strict verdicts, budgets), P1 exact session
> facts, the instrumentation half of P1 observer cost, the improved
> overview, and Views 1, 2, 3, and 5. The validation set and overhead
> budgets, repeated-run variability, View 4, host context, P2 guidance, the
> specialized Windows views, and the pilot-user study remain open and are
> tracked in [future-plan.md](future-plan.md), the single roadmap.

An honest product assessment and a practical development order for DrainTracer.

Reviewed against **v5.1.1**, on **22 October 2026**. This assessment comes from the source, tests, documentation, and the Windows documentation linked below. It is not a usability study or a new performance benchmark. The gaps described as observed are visible in the implementation; customer demand and the proposed improvements still need real-world validation.

## Is this actually a good product?

**Yes. DrainTracer solves a real problem, and I would keep building it. Its strongest use is investigating sustained resource growth in Windows applications and checking whether a fix helped.**

Launching an application, following its children, watching memory/handles/threads, and exporting a useful record is a coherent workflow. Developers can spend considerable time assembling that evidence manually. A convenient terminal tool can make the investigation accessible earlier, including during development and automated checks.

The foundation is stronger than a simple resource dashboard: DrainTracer already has background sampling, sustained-growth heuristics, warmup, process-tree tracking, attach mode, configurable profiles, JSON/HTML reports, run comparison, headless checks, and pip installation. Preserve these and connect them into a clearer investigation workflow.

However, “help developers optimize their delivered apps” is broader than what the product currently measures. High latency, excessive allocations, inefficient algorithms, expensive queries, and poor throughput need evidence that OS process counters alone cannot provide. Stable but wasteful resource usage can also be expensive without producing a leak signal.

I would position it as:

> **Find sustained resource growth in your Windows app, see which process contributes to it, and verify your fix.**

The process attribution and fix verification parts need strengthening before that whole sentence becomes a dependable promise.

I would also challenge the assumption that developers simply do not care about optimization. Many care when a customer experiences a slowdown, a worker crashes overnight, or infrastructure costs rise. Their constraint is often investigation time and competing priorities. DrainTracer becomes valuable when it reduces that time and gives them evidence they can act on.

There is established competition. [Process Explorer](https://learn.microsoft.com/en-us/sysinternals/downloads/process-explorer) already provides extensive process and handle inspection. [VMMap](https://learn.microsoft.com/en-us/sysinternals/downloads/vmmap) provides detailed memory breakdowns. DrainTracer's opportunity is a convenient, repeatable growth investigation with understandable results. The number of panels will not establish that advantage.

My honest reservation: the code demonstrates a useful tool, but it does not yet demonstrate reliable real-world detection or recurring user demand. Those are the next things to prove.

## 1. Improvements needed, in priority order

### P0 — Make “clear,” “collecting,” and “unknown” mean different things

**Observed:** the live health panel can show “NO ACTIVE LEAK SIGNALS” and resource rows marked “CLEAR” before enough evidence exists for detection. The final session summary already supports an inconclusive outcome, so extend that honesty into the live view.

There is also a measurement issue underneath the UI: inaccessible or unavailable process counters can become numeric zeros before aggregation. Some individual counter helpers return zero after an access failure. A real zero and an unknown measurement must remain distinguishable throughout sampling, detection, reporting, and comparison.

Add per-resource states such as:

- Warming up.
- Collecting evidence, with elapsed time and valid sample count.
- No sustained growth detected in the evaluated window.
- Growth detected.
- Partial coverage, stale data, or unavailable.

Track measurement validity per metric. Exclude invalid observations from slope calculations and define how gaps affect window eligibility. Show which processes could not be measured and how much coverage remains. A short run should end as inconclusive when appropriate.

For headless users, introduce an **optional** policy for failing on insufficient evidence or target failure. Preserve existing default exit-code behavior, and report target outcome, measurement outcome, and monitor/export errors separately.

**Done when:** a healthy process, a too-short run, and an inaccessible process produce visibly different results; access loss and recovery cannot silently masquerade as resource changes.

Evidence: [dashboard health rendering](src/dashboard.py), [sampling and window eligibility](src/leak_detector.py), [session outcomes](src/session.py).

### P0 — Show the memory metric that drives the alert

**Observed:** the main memory card charts RSS, while memory-growth detection uses private bytes. Memory sorting also uses private bytes, but narrower process tables can hide that value.

Make private bytes prominent in the growth investigation view, keep RSS available, and label both consistently across cards, tables, evidence, and exports. A user should be able to see the trend responsible for a warning.

Windows working set and private committed memory describe different things; neither is proof of an application-level allocation leak. Explain that distinction briefly in help. Microsoft's [process memory counter documentation](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex) defines the underlying counters.

**Done when:** the alert, graph, process ranking, and report use clearly named metrics, including in compact layouts.

Evidence: [memory card and process table](src/dashboard.py), [memory signal input](src/leak_detector.py).

### P1 — Attribute growth to a process and its lifecycle

**Observed:** detection primarily evaluates aggregate process-tree totals. The process table shows current values, but alerts do not identify which process contributed the growth. The largest process is not necessarily the growing process.

Keep bounded per-process histories keyed by **PID and creation time**. The runner already checks process identity internally; expose that identity consistently in telemetry and reports. Preserve useful summaries for processes that exit.

Rank contributions over the same interval using private-byte growth, handle growth, and thread growth. Distinguish growth within an existing process from growth caused by adding workers. Show starts, exits, restarts, and process count alongside the totals.

**Done when:** in a parent with several workers, a developer can identify the growing worker, inspect the relevant interval, and still find its evidence after it exits. New worker capacity should not automatically be described as a leak.

Evidence: [snapshot and alert models](src/events.py), [process identity tracking](src/process_runner.py), [aggregation](src/leak_detector.py).

### P1 — Make “my fix helped” a defensible conclusion

**Already present:** report comparison and regression checks. Improve their trustworthiness before building another comparison engine.

**Observed:** comparison can warn about incompatible runs while still producing improvement/resolution labels. Reports also lack enough workload and build provenance to establish that two experiments were equivalent.

Record optional workload ID, application build, input size or operation count, duration, warmup, sampling settings, and relevant environment details. Redact sensitive arguments and environment values. Offer a strict comparison mode that returns “not comparable” or “insufficient coverage” when necessary.

Compare repeated equivalent workloads and show variability. Include absolute resource budgets as well as growth: a program can have stable but excessive memory use. Add throughput or latency only through an explicit workload harness or application instrumentation; process counters cannot infer requests served.

**Done when:** a shorter or lighter workload cannot earn an unqualified “fixed” result. A genuinely comparable run shows the measured difference, coverage, and relevant caveats.

Evidence: [comparison rules](src/report_diff.py), [diff command](src/diff_command.py), [report metadata](src/session.py).

### P1 — Preserve exact session facts when chart history is reduced

**Observed:** the session tracks peaks and worst severity, but exported analysis also derives results from retained snapshots. Bounded, thinned history can lose individual peaks or episodes during long sessions.

Export authoritative peak values/timestamps, worst severity per resource, evaluated duration, coverage, and episode counts independently of chart history. Keep those summaries bounded. Mark chart decimation and any dropped detail honestly.

**Done when:** a long session with repeated warning/recovery cycles retains its exact summary facts after export and reopening, even when the chart stores fewer points.

Evidence: [session accumulation and exports](src/session.py), [bounded history](src/history.py), [report analysis](src/analysis.py).

### P1 — Prove detection quality and measure observer cost

Existing tests and synthetic fixtures are valuable. They do not establish how often a real application will trigger a misleading warning or how much overhead monitoring introduces.

Create a small repeatable validation set: steady workload, cache growth followed by a plateau, startup spikes, worker churn, slow sustained leaks, handle/thread growth, access loss, and large process trees. Measure detection delay, missed known cases, misleading warnings, collection time, and monitor CPU/memory. Set budgets from those measurements.

Expose requested versus actual sample cadence, sample age, collection duration, and dropped observations. Keep expensive classification at a bounded lower cadence. Sampling must remain on the background thread.

**Done when:** release notes can describe tested scenarios and measured limitations without claiming universal detection accuracy or inventing a confidence percentage.

### P2 — Help users take the next investigative step

A warning should explain what grew, where, over which interval, and what to examine next. Offer a compact investigation summary suitable for an issue or bug report, with sensitive paths/arguments handled deliberately.

Provide contextual handoff guidance to memory or CPU profiling. [Windows Performance Recorder](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/windows-performance-recorder) records ETW traces, and [Windows Performance Analyzer](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/windows-performance-analyzer) supports deeper trace analysis. Runtime-specific profilers can provide allocation or code-level evidence that DrainTracer does not currently collect.

Avoid promising a source-code root cause from resource slopes alone.

## 2. Relevant TUI windows and views

Use a compact overview plus focused views. Showing every diagnostic simultaneously would make a small terminal harder to use. The following order reflects investigation value.

### First, improve the existing overview

Keep the current resource cards, process tree, health, and event log. Add evidence readiness, measurement coverage, private-byte visibility, sample freshness, and process count. Clearly separate current status from the worst status seen during the session.

This is the highest-priority UI work because every user sees it.

### View 1 — Process inspector and growth leaders

**Question answered:** “Which process deserves my attention?”

Provide a selectable, scrollable, searchable process tree and a ranking by growth over the selected interval. The current truncated process list can hide the process a user needs to investigate.

For the selected process, show identity/lifetime, parent, status, private bytes and RSS, signed growth and slope, handles, threads, CPU, I/O rates, and recent lifecycle events. Keep tree totals visible as context. Preserve an exited-process summary.

**Data work:** current counters already exist; durable identities, per-process trend summaries, and exited-process retention need additional telemetry/model work.

### View 2 — Signal evidence / “Why this warning?”

**Question answered:** “What evidence supports this conclusion?”

Expand the existing alert details into a focused explanation: evaluated resource, metric, window, valid sample coverage, baseline/current value, delta, slope, configured thresholds, and why the window did or did not qualify.

Expose the actual monotonic-growth and drop checks used by the detector. Label severity as a heuristic assessment. Do not turn a heuristic score into a probability of a leak.

**Data work:** slope/window/delta already exist in alerts; other evaluation details currently stay inside the detector and should be emitted as immutable diagnostic data. The UI should not recalculate the detection algorithm.

### View 3 — Expanded timeline and investigation markers

**Question answered:** “When did growth start, and what happened around it?”

Show one readable resource chart at a time, with a selectable tree/process scope, time range, baseline marker, and annotations for warmup, process starts/exits, access gaps, pauses, and warning/recovery transitions. Let the user mark “started workload” or “opened document.”

Keep decimation visible. Avoid implying that a coarse chart contains every original sample.

Add a way to freeze the **view** independently. The existing Space control pauses the target and sampling; silently changing that behavior would distort investigations.

**Data work:** aggregate history exists; markers and process-level history need extensions. Markers establish timing, not proof of causation.

### View 4 — Saved-run comparison

**Question answered:** “Did this change improve the same workload?”

Load a baseline report and show comparability first, then changes in growth, resource peaks, coverage, and signal episodes. Show workload-normalized results only when the required workload data exists.

Reuse the report-diff backend. Build this view after strict comparison semantics are dependable; attractive improvement labels can otherwise encourage the wrong conclusion.

### View 5 — Monitor diagnostics and system context

**Question answered:** “Can I trust this recording, and was the machine under pressure?”

Show sampling cadence, collection duration, sample age, inaccessible processes, dropped data, classification freshness, and monitor CPU/memory. Add host CPU and available memory/commit context when collected reliably.

Keep a small coverage/freshness indicator on the overview and put the details here.

**Data work:** much of this needs explicit new instrumentation and host sampling. Host pressure is context, not proof that the target caused a problem.

### Specialized Windows views — add when real users need them

- **GDI and USER objects:** useful for Windows GUI applications leaking drawing resources or UI objects. These are additional counters, separate from existing kernel-handle categories. [GetGuiResources](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getguiresources) provides process counts subject to access/session constraints; handle failure distinctly from a genuine zero.
- **Thread activity and churn:** identify recurring thread creation and threads consuming CPU. This needs new per-thread sampling. Low CPU alone cannot establish a deadlock or explain blocking.
- **I/O detail:** expand existing per-process read/write rates when file-heavy workloads need it. Do not label general process I/O as network throughput or request latency.
- **Handle details:** deepen the existing handle-type breakdown before adding another total-handle gauge. Detailed object inspection should be bounded and opt-in where expensive.

These are audience-specific additions. GPU panels, network latency, heap allocation trees, and automatic optimization advice should wait for demonstrated use cases and appropriate data collection.

### Navigation and layout requirements

Proposed interaction: numbered views, Tab to change focus, Up/Down to select/scroll, Enter for details, and Escape to return. Check collisions with existing controls. Windows extended-key decoding needs implementation for arrow navigation.

At 80×24, prioritize the active view with a compact persistent status line. At larger sizes, show supporting context. Verify resizing, long process names, denied access, exited selections, and legacy console characters.

Keep monitoring independent of the selected view. Selecting one process must not silently change the existing tree-wide kill action. Preserve confirmation behavior and make the action scope explicit.

## Recommended implementation order

1. **Trust the overview:** per-metric validity, collecting/inconclusive states, consistent private-byte display, and focused regression checks.
2. **Find the contributor:** process identities in snapshots, bounded growth histories, lifecycle summaries, and a navigable inspector.
3. **Explain the result:** evidence view and exact exported summaries; add timeline markers where they clarify real investigations.
4. **Verify a fix:** experiment metadata, strict comparisons, absolute budgets, and then a comparison view.
5. **Expand selectively:** measured overhead diagnostics and specialized counters based on pilot-user needs. Start collecting overhead measurements earlier while implementing the preceding work.

Keep the backend/UI separation: sampling and evaluation produce data; the TUI renders it. Preserve sustained-window detection rather than replacing it with single-sample alarms. Do not change heuristic thresholds casually alongside presentation work.

Reconcile [future-plan.md](future-plan.md) with shipped releases before treating it as the next-work checklist. Some installation/packaging/CI items are already delivered, and several trust/comparison ideas are already planned there. This document ranks the immediate work; it should not create a competing roadmap.

## How to know whether this deserves more investment

Recruit 5–10 Windows developers with real long-running workers, desktop apps, or multiprocess tools. That is a proposed pilot size, not evidence of demand. Watch them investigate their own problems with minimal coaching.

Record whether they can identify the contributing process, understand the evidence, export a useful report, and verify a change. Ask what they would have used otherwise. Track time to useful evidence, misleading signals, missed known problems, and whether they voluntarily use it again.

If they repeatedly find actionable problems or shorten an investigation, you have a strong reason to continue. If they only admire the dashboard, change the workflow before adding more panels. Stars and downloads are useful distribution signals, but repeat investigations and fixes are stronger evidence of product value.

**My recommendation:** keep building DrainTracer. Put the next effort into credible measurements, process attribution, and reproducible fix verification. Those improvements would make it substantially more useful than another collection of live resource gauges.
