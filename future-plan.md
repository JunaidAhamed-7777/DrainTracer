# Plans for the future

This file is DrainTracer's single roadmap. It preserves the original
milestones and the versions in which they landed, records what has shipped
since, and lists the remaining proposed work, with no promised release
dates. See the [README](README.md) for the current feature overview,
[RELEASES.md](RELEASES.md) and [CHANGELOG.md](CHANGELOG.md) for shipped
changes, and [CONTRIBUTING.md](CONTRIBUTING.md) to propose or help with a
change.

**Reconciled 2026-10-31** against the linked implementations and recorded
verification below. Published versions remain in [RELEASES.md](RELEASES.md);
merged implementation, candidate validation and published-package validation
are separate states. Historical measurements retain their exact source and
candidate version; they are not relabeled as a later release's results.
The assessment in [Immediate additions.md](Immediate%20additions.md) is folded
in here, with broader unmet goals kept open even when a scoped issue closes.

## Delivered milestones

1. **Dedicated Report Directory & Unique IDs** — done in v0.8
   - Save reports to a dedicated `leak_reports/` folder.
   - Assign a unique UUID to each report (displayed in the TUI on generation) and format filenames as `leak_report_<UUID>.json`.
   - Embed ISO 8601 creation timestamps at the root level of the JSON report.

2. **Target Attachment (`--pid`)** — done in v0.9
   - Support attaching DrainTracer to an already running process using its Process ID (`python src/main.py --pid 4820`).
   - *Real-world use case:* Debugging background services, web servers (e.g., FastAPI, Node.js), or long-running database daemons without needing to restart them.

3. **CI/CD & Headless Test Mode (`--headless` / `--timeout`)** — done in v1.0
   - Add a non-interactive CLI flag that runs target processes for a set duration (e.g., `--timeout 60s`) without launching the Rich TUI.
   - Return exit code `1` if any `HIGH` or `CRITICAL` leak threshold is breached during execution by default; `--fail-on` selects the lowest failing severity.
   - *Real-world use case:* Running automated leak regression tests in GitHub Actions pipelines before merging Pull Requests.

4. **Interactive HTML & Visual Chart Reports** — done in v1.1
   - Generate self-contained HTML reports alongside JSON exports (`leak_report_<UUID>.html`) featuring interactive line graphs with an embedded chart implementation and no external dependencies or network requests.
   - Visualize memory, handle, and thread trends over time with highlighted red markers where alerts were triggered.

5. **Granular Handle Classification** — done in v1.2
   - Categorize Windows handles into specific types (File handles, Socket/Network handles, Registry keys, Thread/Process handles, Mutexes) rather than just a total count.
   - *Real-world use case:* Identifying whether an application is leaking unclosed file streams vs. unclosed database socket connections.

6. **Custom Configuration Profiles (`.drainrc` / `config.toml`)** — done in v1.3
   - Allow users to define custom leak thresholds and window durations per project.
   - *Real-world use case:* Game engines or heavy data-processing pipelines legitimately require higher baseline memory slope tolerances than lightweight utility scripts.

7. **Warm-Up / Calibration Phase** — done in v1.4
   - Add an initial startup grace period (e.g., `--warmup 10s`) during which resource sampling occurs but leak detection heuristics are deferred.
   - *Real-world use case:* Preventing false-positive alerts triggered by standard application initialization, cache warming, or JIT compilation spikes.

8. **Report Comparison & Diffing Tool (`python src/main.py diff`)** — done in v1.5
   - Add a CLI command to compare two JSON leak reports: `python src/main.py diff reportA.json reportB.json`.
   - Highlight whether a patch reduced memory drift rates or resolved handle leaks between software versions.

9. **Release-based Semantic Versioning** — done in v5.0.0
   - One version per published release, owner-only major bumps, and a
     mandatory runtime-snapshot comparison before and after every change.

10. **Installable package and `draintracer` command** — done in v5.1.0
    - pip installation, wheel and source builds with bundled assets, the
      `draintracer` command, `python -m draintracer`, and `--version`, while
      keeping `python src/main.py`.

11. **PyPI distribution and release workflow** — done in v5.1.1
    - A Windows GitHub Actions workflow that checks the runtime snapshot,
      compiles, runs the unit suite, builds and verifies the wheel in a clean
      environment, and publishes with Trusted Publishing.

12. **Trustworthy, attributable, verifiable evidence** — done in v5.2.0
    - *Validity and evidence states:* unreadable counters are unknown, not
      zero; per-metric coverage; per-resource collecting / no sustained
      growth / growth / unavailable / stale states; separate target,
      measurement, and monitor-error outcomes; optional
      `--fail-on-inconclusive` and `--fail-on-target-error` policies.
    - *Private bytes first:* the memory card, compact strip, narrow tables,
      alerts, reports, and help name private bytes as the alerted metric,
      with RSS (working set) as context.
    - *Process attribution:* PID-plus-creation-time identity, bounded
      per-process histories, ranked contributors per alert, growth from new
      workers separated from growth inside existing processes, and logged
      starts, exits, and restarts with exited-process summaries.
    - *Exact facts and monitor diagnostics:* `session_facts` independent of
      chart thinning, marked decimation, cadence, collection cost, dropped
      samples, and the monitor's own CPU and memory.
    - *Defensible comparisons:* redacted experiment provenance, classified
      caveats with qualified verdicts, `diff --strict`, whole-run peaks,
      growth per operation, and absolute resource budgets.
    - *Focused views:* process inspector, signal evidence, timeline with
      markers and a view-only freeze, and monitor diagnostics, with decoded
      arrow and paging keys.

13. **Windows and Linux monitoring** — implemented in v5.4.0 development source (unpublished)
    - Linux launch/attach/control semantics, USS private memory, open-file-
      descriptor counts and classification, terminal controls and offline
      reports, with platform definitions preserved in comparisons.
    - Windows and Linux CI, real-process/terminal verification and clean
      distributions. WSL is tested; this is not evidence for macOS or
      unattended enterprise/service-account deployments.

Later hardening improves process identity checks, orphan discovery,
bounded report history, error handling, and literal argument forwarding to
Windows batch targets. The current behavior is documented in
[working-principle.md](working-principle.md).

## Topics for discussion

- **Release verification:** pull-request matrices, bounded real-process cases,
  retained diagnostics and protected publishing checks are implemented (T1).
  The maintained consumer action still needs its documented package pin
  published and installed from PyPI before W3 closes.
- **Calibration and cost:** repeated growth/non-growth results and profile
  guidance are recorded (T3). The full observer's frozen timing ceilings
  failed verification (T5); separate host component passes do not erase them.
- **First use and demand:** tested guides and accessible evidence are available,
  while uncoached first-time acceptance and consented user feedback remain
  open (D3/O2/I6). Synthetic exercises do not establish demand.
- **Optional sharing:** local access controls and recovery references are
  implemented. The prepared controlled TLS/CLI slice is separate from hosted
  comparison pages, pilot-derived objectives or a hosted availability promise.
- **Deeper attribution:** the representative worker-growth policy is decided
  (O3). A future tracer needs demonstrated need, privileges/privacy review
  and measured adapter cost; no adapter has been selected or claimed measured.

### Open original workstreams and bounded starters

Five original human/demand workstreams remain beyond the current delivery
queue: [#31 first-time walkthrough acceptance](https://github.com/JunaidAhamed-7777/DrainTracer/issues/31),
[#49 consent-based pilot](https://github.com/JunaidAhamed-7777/DrainTracer/issues/49),
[#50 demand-led Windows diagnostics](https://github.com/JunaidAhamed-7777/DrainTracer/issues/50),
[#51 pilot-informed notifications](https://github.com/JunaidAhamed-7777/DrainTracer/issues/51)
and [#53 broader deployment demand](https://github.com/JunaidAhamed-7777/DrainTracer/issues/53).
Publication/integration gates for W3/#44 and the scoped S2/#48 implementation
remain separate from those five. The broader T5, S2 and O1 goals below are
also not converted into passes by closing narrower implementation issues.

The ten additional good first issues are bounded contributions with verified
starting points and acceptance checks, not substitutes for those goals:

- [#130 exclude stale modules from direct wheels](https://github.com/JunaidAhamed-7777/DrainTracer/issues/130).
- [#131 reject ambiguous Windows recipe environment names](https://github.com/JunaidAhamed-7777/DrainTracer/issues/131).
- [#132 workload command version flag](https://github.com/JunaidAhamed-7777/DrainTracer/issues/132).
- [#133 preserve operation kind in queue history](https://github.com/JunaidAhamed-7777/DrainTracer/issues/133).
- [#134 bounded report stdin](https://github.com/JunaidAhamed-7777/DrainTracer/issues/134).
- [#135 detached snapshot JSON copy](https://github.com/JunaidAhamed-7777/DrainTracer/issues/135).
- [#136 operating command version flag](https://github.com/JunaidAhamed-7777/DrainTracer/issues/136).
- [#137 literal backticks in summary report paths](https://github.com/JunaidAhamed-7777/DrainTracer/issues/137).
- [#138 separate agent request timeout](https://github.com/JunaidAhamed-7777/DrainTracer/issues/138).
- [#139 non-launching recipe validation](https://github.com/JunaidAhamed-7777/DrainTracer/issues/139).

Choose an existing scoped issue where possible. Propose substantial additional
work with its workload, expected behavior and bounded verification first.

## Proposed delivery plan: a dependable developer service

Planning update: **2026-10-31**. The checklist below extends the discussion
topics into work that can be scoped, implemented, and verified. Phase names
indicate dependency order, not promised dates or release versions. Keep the
delivered milestones above as history.

Status as of 2026-10-31: a checked item has its linked technical implementation
and recorded verification, with the scope qualifications below. An unchecked
item retains an unmet goal or publication/pilot gate even if a narrower leaf
issue is complete. Original **Done when** criteria are preserved. No phase
name assigns a release date or speculative feature version.

The goal is a tool developers can install quickly, use during an
investigation, run repeatedly in CI, and eventually access through an
optional service. Start with reliable local checks and evidence. Build
service capabilities around those same contracts so teams can adopt them
without changing how their applications run.

### Starting point and product boundaries

DrainTracer supports installed and source-based Windows and Linux use,
launch/PID attachment, recursive process tracking, warm-up, profiles,
headless checks, offline HTML/JSON and strict/repeated comparisons. The
shared backend has a documented Python API, an opt-in per-user Windows
agent, private local team storage, and pull-request/release verification.
These strengthen the original evidence and ownership contracts. There is
no hosted production service or automatic report upload.

Linux core support is implemented in v5.4.0 development source, an unpublished
candidate with its own process and metric semantics; it does not inherit
Windows counter meanings. Keep the offline
CLI useful without an account. Execution remains on a developer-controlled
machine or organization-owned runner. macOS, unattended Windows service
accounts and broader enterprise deployments need their own evidence and
maintainers under O4.

An alert remains evidence of sustained growth, not proof of an allocation
leak. A short, inaccessible, or interrupted run must remain distinguishable
from a completed check with sufficient coverage. Preserve background
sampling, bounded resource use, PID identity checks, and the ownership
distinction between launched and attached targets throughout this plan.

### Phase 1 — Establish trustworthy results and releases (P0)

- [x] **T1. Automate Windows verification and release checks.** Add a
  repository workflow for compilation, the existing unit suite, report
  script syntax checks, and bounded real-process cases. Define and test
  supported Python/Windows combinations and minimum/current dependencies.
  Separate fast pull request checks from longer soak and release runs;
  retain diagnostic artifacts on failure with a documented expiry.
  **Done when:** a deliberate detector, report, or cleanup regression blocks
  release, and a clean Windows machine can reproduce the checks from the
  documented source commit.
  **Status:** [Issue #22](https://github.com/JunaidAhamed-7777/DrainTracer/issues/22)
  and the [development checks](docs/development.md) cover Windows/Linux
  pull-request matrices, bounded real-process cases, failure diagnostics and
  clean-distribution verification before publishing. Passing CI is not a
  claim that every local performance ceiling or workload policy passed.

- [x] **T2. Make automated outcomes explicit.** Define a structured session
  result that separates signal severity, per-resource detection coverage,
  target exit status, monitor/report errors, interruption, and cleanup
  outcome. Add opt-in CI policies for inconclusive checks and failed targets.
  Today an inconclusive headless run can exit `0`; preserve existing exit
  behavior by default and document policy precedence and any migration.
  **Done when:** tests distinguish a warm-up-only run, partial access,
  target exit `23`, interruption, and a signal followed by an export error,
  without automation parsing terminal messages or treating missing data as
  a measured zero.
  **Status:** [Issue #23](https://github.com/JunaidAhamed-7777/DrainTracer/issues/23)
  and the [headless guide](docs/usage.md#run-without-a-dashboard) define typed
  startup/cleanup, target, measurement and monitor/export outcomes. Full
  coverage and inconclusive/target-error policies are explicit opt-ins;
  existing failure precedence and unknown counters remain qualified.

- [x] **T3. Calibrate detection against repeatable workloads.** Extend the
  existing bounded fixtures with slow growth, stable caches, repeated
  allocation/recovery, worker churn, and partially inaccessible trees.
  Record workload inputs, machine conditions, settings, valid coverage,
  detection latency, and observed false alarms or missed growth. Test
  repeated runs before changing defaults; explain which profiles fit which
  workloads rather than automatically learning away a leak.
  **Done when:** each proposed threshold change has growth and non-growth
  evidence, a documented minimum useful run length, and known limitations.
  **Scope from the product assessment:** the validation set should cover a
  steady workload, cache growth followed by a plateau, startup spikes,
  worker churn, slow sustained leaks, handle and thread growth, access loss,
  and large process trees, and record detection delay, missed known cases,
  and misleading warnings. Release notes should then describe tested
  scenarios and measured limitations without claiming universal accuracy
  or a confidence percentage.
  **Status:** [#24 / PR #124](https://github.com/JunaidAhamed-7777/DrainTracer/pull/124)
  supplies 72 source-pinned native-Windows/WSL runs across twelve scenarios,
  repeated three times per platform. [Results](docs/calibration-results.md)
  retain benign cache/startup warnings and slow-growth HIGH-policy misses.
  Minimum-duration/profile guidance is scoped to those observations; no
  threshold change, universal accuracy or native-Linux-host claim is made.

- [x] **T4. Preserve authoritative evidence across long runs.** Keep compact
  run summaries, strongest observed signals, and coverage accounting
  independently of thinned chart history. Expose observed/retained sample
  counts, event truncation, queue drops, and gaps. Distinguish exact summary
  fields from estimates computed from retained samples, using the same
  definitions in JSON, HTML, and comparisons. Evaluate bounded checkpoint
  storage for interrupted sessions.
  **Done when:** a long-run test stays within recorded memory/disk limits,
  preserves its strongest signal after thinning, and produces consistent,
  clearly qualified summaries after partial recovery.
  **Status:** [#25 / PR #103](https://github.com/JunaidAhamed-7777/DrainTracer/pull/103)
  adds bounded same-session checkpoints and qualified partial recovery.
  Producer-owned facts survive queue eviction, and exact event-log eviction
  counts remain separate from chart thinning. [Report guidance](docs/reports.md)
  distinguishes complete facts, retained detail and interrupted prefixes.

- [ ] **T5. Measure overhead and stress lifecycle safety.** Benchmark
  sampling time, CPU, memory, handle classification, queue lag, and export
  cost across tree sizes. Set explicit budgets from those measurements.
  Isolate slow subscribers and exports from sampling; expose dropped data
  rather than silently implying completeness. Exercise PID reuse, short-lived
  parents, access denial, sleep/resume, clock changes, stalled consumers,
  full disks, and shutdown during export. Document discovery limits and
  the cleanup policy for an unexpected monitor crash.
  **Done when:** repeatable stress and soak runs meet the agreed budgets,
  cleanup failures are visible, and attached targets are never controlled
  using a stale PID identity or left paused by ordinary shutdown.
  **Status:** the scoped measurement/safety work in
  [#26 / PR #127](https://github.com/JunaidAhamed-7777/DrainTracer/pull/127)
  records reference-derived budgets, repeated 1/7/31-process measurements,
  failure drills and two 30-minute soaks. The broader budget-pass criterion
  is **not met**: both [verification sets](docs/observer-verification-results.md)
  retain `budget_failures`, seven timing-ceiling breaches, and an unexpected
  Windows thread signal. Functional/reader/owned-cleanup quality and CPU/RSS
  ceilings passed; they do not excuse failed maxima/P95s. Physical sleep/wake,
  forced kernel PID reuse and production throughput are not established.
  Preserve the frozen values and source identities; do not rerun or widen
  ceilings merely to obtain a pass.

**Phase gate:** T1–T5 establish the evidence needed to recommend DrainTracer
as a regression gate. Installation and documentation work can proceed in
parallel; claims about reliable checks must wait for measured results.
**Gate status:** not fully met. T5's recorded timing failures remain a failed
performance-acceptance result even though its scoped measurement/safety issue
is complete. Use explicit workload/machine budgets rather than a universal
reliability claim.

### Investigation workflow follow-ups (P1, from the product assessment)

These follow-ups from [Immediate additions.md](Immediate%20additions.md)
supplement milestone 12; their statuses distinguish implemented capabilities
from remaining validation. The validation set and observer-cost budgets are
tracked under T3 and T5, and the pilot study under O2. Keep
sampling and evaluation on the detector thread and the TUI as a renderer;
do not change heuristic thresholds alongside presentation work.

- [x] **I1. Saved-run comparison view (assessment View 4).** Load a
  baseline report in the dashboard and show comparability first, then
  changes in growth, peaks, coverage, and signal episodes, reusing
  `report_diff` and its strict semantics. Show per-operation results only
  when both runs recorded operations.
  **Done when:** a non-comparable baseline shows its caveats before any
  improvement label, and the view's verdict matches `draintracer diff`.
  **Status:** [#35 / PR #115](https://github.com/JunaidAhamed-7777/DrainTracer/pull/115)
  adds a bounded background-loaded dashboard Compare view using the shared
  strict engine. Caveats precede verdicts, errors preserve the prior result,
  and continued real collection plus narrow/literal rendering are tested.

- [x] **I2. Repeated runs and variability.** Accept several equivalent
  reports per side and show the spread of drift and peaks. One rule to
  evaluate: count a change as improved only when the two sides' ranges do
  not overlap.
  **Done when:** noisy repeated runs cannot produce an unqualified
  improvement from a single favorable pair.
  **Status:** [#36 / PR #110](https://github.com/JunaidAhamed-7777/DrainTracer/pull/110)
  compares 2–20 independent runs per side using shared conservative ranges.
  Every within/across-group pair is checked; copied identities and excessive
  group input are refused. Observed min/max ranges are descriptive, not
  confidence intervals; W4 reuses the same engine.

- [x] **I3. Host context for the monitor view (assessment View 5).** Sample
  host CPU and available memory/commit at a bounded cadence and show it as
  context, never as proof that the target caused pressure.
  **Done when:** the monitor view and reports show host pressure with its
  sampling age, and collection stays within the T5 budgets.
  **Status:** [#27 / PR #113](https://github.com/JunaidAhamed-7777/DrainTracer/pull/113)
  samples CPU/available physical memory every five seconds with unknowns,
  thread priming and monotonic published age. The separate
  [host component proof](docs/host-budget-results.md) passes three frozen-
  budget verification replicates per OS at source `68442ce`, retaining the
  first provenance failure and every reference attempt. Target collection
  timing excludes this host call; full T5 timing failures remain unchanged.

- [x] **I4. Timeline and marker follow-ups.** A per-process scope and a
  selectable time range beyond the last 240 samples, a label prompt for
  `M` markers (`add_marker(label)` already exists), and markers drawn in
  the HTML report.
  **Done when:** a user can mark "opened document", find it in the timeline
  for one process, and see it in the exported HTML.
  **Status:** [#28 / PR #114](https://github.com/JunaidAhamed-7777/DrainTracer/pull/114)
  implements identity-scoped retained timelines, selectable ranges and literal
  named markers in HTML. Text entry isolates target controls, and retained/
  thinned history plus view freeze remain distinct from sampling pause.

- [x] **I5. Investigation summary and next-step guidance (assessment P2).**
  A compact, redacted summary of what grew, where, and over which interval,
  suitable for an issue or bug report, with handoff guidance to Windows
  Performance Recorder/Analyzer or runtime-specific profilers. Never promise
  a source-code root cause from resource slopes.
  **Done when:** a summary pasted into an issue names the resource, metric,
  contributing process, interval, and coverage, without local secrets.
  **Status:** [#42 / PR #106](https://github.com/JunaidAhamed-7777/DrainTracer/pull/106)
  and the [investigation guide](docs/investigation-summary.md) provide a typed
  redacted summary with resource/interval/coverage and profiler handoff.
  Bounded identity-free evidence is reused; allocation cause is not inferred.

- [ ] **I6. Specialized Windows views, when real users need them.** GDI and
  USER object counts (via `GetGuiResources`, with failure kept distinct from
  a genuine zero), thread activity and churn (needs per-thread sampling;
  low CPU alone does not establish a deadlock), deeper per-process I/O
  detail (never labeled as network throughput or latency), and bounded,
  opt-in handle detail beyond the existing type breakdown. GPU, network
  latency, heap allocation trees, and automatic optimization advice wait
  for demonstrated use cases.
  **Done when:** each added view answers a need recorded from a pilot user
  (O2) and has a measured collection cost.
  **Status:** [#50](https://github.com/JunaidAhamed-7777/DrainTracer/issues/50)
  remains open. No pilot need, specialized view cost or allocation diagnosis
  is manufactured from the technical fixture sets.

- [x] **I7. Remaining consistency gaps.** Smaller issues found while
  delivering milestone 12, reconciled on 2026-11-01 in the
  [linked implementation/decision record](docs/consistency-tracking.md).
  No-window wording, unknown counters, HTML/strict coverage, headless live
  counts, whole-number budgets, help isolation and configured-budget display
  have linked fixes. This issue adds independent within-run process rankings
  to analysis/diffs and explicit positive-growth arithmetic labels. Detailed
  lifecycle text remains intentionally bounded, with exact eviction counts;
  it is not a complete process audit or a new temporal rate limiter.
  Report-sourced comparison budgets and controlled baseline checks are
  delivered under [#37 / PR #140](https://github.com/JunaidAhamed-7777/DrainTracer/pull/140),
  merged before this reconciliation. Explicit command flags override the
  corresponding recorded budget. The [#29](https://github.com/JunaidAhamed-7777/DrainTracer/issues/29)
  umbrella preserves separate human acceptance and broader performance gates.
  **Done when:** each gap is fixed with a regression test or recorded as an
  intentional limitation in the relevant guide.
  **Status:** [#29 / PR #129](https://github.com/JunaidAhamed-7777/DrainTracer/pull/129)
  and the [implementation/decision record](docs/consistency-tracking.md)
  reconcile the original gaps, including delivered report-sourced budgets
  under [#37](https://github.com/JunaidAhamed-7777/DrainTracer/issues/37).
  Positive-growth arithmetic, independent bounded process rankings and the
  deliberate lifecycle-detail retention limit are explicit. This completion
  does not resolve any human acceptance or broader performance goal.

### Phase 2 — Make installation and everyday use accessible (P1)

- [x] **D1. Ship an installable package and console command.** Delivered
  in v5.1.0 and v5.1.1 (see [RELEASES.md](RELEASES.md)). Add package
  metadata, a wheel/source build, and a proposed `draintracer` entry point
  while preserving source execution. Bundle `TITLE.txt` and required report
  resources correctly, and separate the monitor's Python environment from
  the target interpreter when explicitly selected. Publish tested upgrade,
  uninstall, compatibility, and release-checksum instructions.
  **Done when:** a standard user outside the checkout can install a release,
  request help, monitor a bounded sample, export both report formats, and
  uninstall without Git, path editing, or routine elevation.

- [x] **D2. Add preflight diagnostics and project setup assistance.** Design
  proposed diagnostic, configuration-validation, and project-init commands.
  Check target/interpreter paths, settings precedence, report-directory
  access, permissions, and whether timeout/warm-up leave enough coverage.
  Show resolved settings and actionable fixes before launching; preview
  generated configuration and avoid overwriting existing project files.
  **Done when:** common setup failures can be diagnosed without starting or
  pausing a real application, and generated profiles pass the same validator
  used by normal monitoring.
  **Status:** [#30](https://github.com/JunaidAhamed-7777/DrainTracer/issues/30)
  and the [preflight guide](docs/preflight.md) document `doctor` and preview-
  first `init`. Configuration/path/coverage checks do not start a target;
  existing files and checkpoint destinations remain protected.

- [ ] **D3. Provide a guided first useful result and practical recipes.**
  Build a short walkthrough using the included steady and growth fixtures:
  install, run, interpret, export, apply a fix, and compare. Add tested
  recipes for a Python worker, native executable, batch launcher, and an
  already-running Windows service. Each recipe states permissions, workload
  exercise, warm-up, duration, expected outcome, and cleanup behavior.
  **Done when:** developers unfamiliar with the repository can complete the
  walkthrough and explain clear, signal, and inconclusive outcomes without
  maintainer intervention.
  **Status:** [#57](https://github.com/JunaidAhamed-7777/DrainTracer/issues/57),
  [#58](https://github.com/JunaidAhamed-7777/DrainTracer/issues/58),
  [#59](https://github.com/JunaidAhamed-7777/DrainTracer/issues/59) and
  [#60](https://github.com/JunaidAhamed-7777/DrainTracer/issues/60) provide the
  technically checked [walkthrough](docs/first-result.md),
  [Python worker](docs/python-worker.md), [native/batch](docs/native-and-batch.md)
  and [service observation](docs/service-observation.md) recipes. Service CI
  verifies same-identity survival after detach and owned registration cleanup.
  [#31](https://github.com/JunaidAhamed-7777/DrainTracer/issues/31) remains open
  until a first-time Windows contributor completes the workflow without
  coaching; maintainer command validation is not that evidence.

- [x] **D4. Offer accessible terminal presentation and controls.** Extend
  current narrow-terminal and legacy-encoding support with explicit color
  controls, a static/plain mode, and quiet output. Keep verdicts and missing
  readings understandable without color or animation; document keyboard
  controls and destructive actions clearly. Evaluate a view-only pause
  separately from the existing control that pauses the target, with an
  explicit migration if control behavior changes.
  **Done when:** keyboard and screen-reader users can start, inspect,
  interpret, export, and quit a session in plain output and at small terminal
  sizes, with no hidden change to target ownership.
  **Status:** [#32](https://github.com/JunaidAhamed-7777/DrainTracer/issues/32)
  delivers `--no-color`, static `--plain` and `--quiet`, preserving literal
  text, unknowns and target ownership. [Usage](docs/usage.md) documents the
  controls; narrow/legacy-codepage and plain-output paths are checked.
  This does not certify every screen-reader client or fulfill the separate
  representative-user phase gate.

- [x] **D5. Make exported evidence accessible without chart interaction.**
  Add equivalent textual summaries and data tables/downloads for HTML
  charts, labeled controls, visible focus, keyboard navigation, meaningful
  headings, and non-color signal cues. Verify zoom, contrast, and assistive
  technology behavior while keeping reports self-contained and offline.
  **Done when:** the verdict, coverage, trend, units, and relevant process
  evidence can all be read without hovering, dragging, or seeing the chart.
  **Status:** [#33 / PR #108](https://github.com/JunaidAhamed-7777/DrainTracer/pull/108)
  and [#62 / PR #107](https://github.com/JunaidAhamed-7777/DrainTracer/pull/107)
  add static evidence tables/CSV, keyboard range/Zoom controls, labels/focus
  and tested light/dark contrast. [Recorded accessibility checks](docs/html-accessibility.md)
  cover semantic fixtures, Chromium and Windows accessibility interfaces,
  assistive Expand/table-focus behavior and a narrow viewport. No universal
  screen-reader certification or unobserved native-key activation is claimed.

**Phase gate:** a fresh Windows environment and representative first-time
users complete the documented workflow using the distributed artifact.
Usability and accessibility failures feed back into D1–D5 before promotion.
**Gate status:** the technical guides and recorded accessibility checks are
available; uncoached first-time acceptance under #31 and representative
consented feedback under #49 remain outstanding.

### Phase 3 — Fit developer workflows and stabilize integrations (P2)

- [x] **W1. Define stable report and machine-output contracts.** Publish a
  machine-readable report schema and session-summary contract with supported
  versions, optional fields, units, structured errors, and compatibility
  fixtures. Validate untrusted input with size/depth limits; reject unknown
  future schemas clearly and preserve unknown values in older reports.
  Add a streaming format if integrations need live events, keeping machine
  records on stdout and diagnostics on stderr.
  **Done when:** a separate consumer can validate supported reports and
  outcomes without Rich, presentation parsing, or undocumented internal
  imports, and malformed reports fail with useful bounded errors.
  **Status:** [#34](https://github.com/JunaidAhamed-7777/DrainTracer/issues/34)
  publishes bounded report/summary schemas, fixtures, typed errors and the
  [validation CLI](docs/report-contract.md). Strict numeric rejection, future-
  schema refusal and unknown legacy values do not depend on parsing Rich.

- [x] **W2. Make workload execution reproducible.** Add explicit target
  interpreter, working-directory, and environment selection with redaction
  rules. Support bounded readiness checks and workload exercise steps so a
  server is measured under actual load after it becomes ready. Offer opt-in
  target log capture with separate files, size limits, and secret handling;
  preserve literal argument forwarding and never route target stdin through
  dashboard controls.
  **Done when:** the same recipe runs locally and in CI with recorded
  provenance, readiness failures are distinct from leak signals, and timeouts
  clean up only the processes that session owns.
  **Status:** [#43 / PR #119](https://github.com/JunaidAhamed-7777/DrainTracer/pull/119)
  and [workload recipes](docs/workloads.md) deliver explicit interpreter/cwd/
  environment choices, readiness, bounded exercises and opt-in redacted logs.
  Selected/inherited secret environment values are not recorded or compared;
  they block strict equivalence. Clean recipes state their limited platform-
  basics equivalence. Workload failure and owned cleanup remain explicit.

- [ ] **W3. Supply a maintained Windows CI template and reusable action.**
  Build on existing annotations and job summaries with version-pinned setup,
  strict outcome policies, workload execution, and report retention even on
  failure. Explain runner permissions, costs, time limits, and artifact
  privacy. Keep untrusted pull request jobs away from publishing credentials
  and approved-baseline writes.
  **Done when:** an example pull request displays a useful leak comparison,
  a short inconclusive check fails under the selected policy, and a failing
  job retains enough evidence to reproduce its result.
  **Status:** [#44 / PR #120](https://github.com/JunaidAhamed-7777/DrainTracer/pull/120)
  has the maintained [action/template](docs/ci-workloads.md) and a successful
  [hosted candidate example](https://github.com/JunaidAhamed-7777/DrainTracer/actions/runs/41207484179)
  with comparable evidence, opted-in short-run failure and retained artifacts.
  Keep this item open until the documented exact package pin is published
  and installed from PyPI. A candidate wheel/hosted fixture is not a
  published-package installation check.

- [x] **W4. Turn comparisons into controlled baseline checks.** Extend
  existing `diff` with workload, source commit, settings, detector version,
  environment, and coverage compatibility checks. Use repeated measurements
  and explicit absolute/relative budgets where justified by T3. Require
  deliberate baseline approval; make suppressions scoped, justified, and
  expiring rather than weakening global defaults.
  **Done when:** a known regression fails reproducibly, incompatible or
  insufficient runs cannot silently replace a baseline, and an approved
  update records who changed it and why.
  **Status:** [#37](https://github.com/JunaidAhamed-7777/DrainTracer/issues/37)
  and [controlled baselines](docs/controlled-baselines.md) implement repeated
  eligibility, report-budget/flag precedence, audited deliberate promotion
  and scoped expiring exceptions. Missing provenance, paused/gapped/adjusted
  sampling and any resource's overlap/unknown evidence cannot be waived;
  an exception never changes an inconclusive result into a pass.

- [x] **W5. Publish a small supported Python API.** Expose typed session
  options/results, launch/attach, cancellation, snapshots/events, and report
  export through the shared backend. Remove requirements for SDK consumers
  to import dashboard classes or initialize a terminal. Define lifecycle
  ownership, context-managed cleanup, callback isolation/backpressure, and
  compatibility policy before promising a stable interface.
  **Done when:** an external script or test fixture integrates through the
  documented API, handles partial failure, cancels safely, and cannot stall
  sampling with a slow subscriber.
  **Status:** [#38 / PR #109](https://github.com/JunaidAhamed-7777/DrainTracer/pull/109)
  publishes the [Python session API](docs/python-api.md), immutable snapshots,
  explicit launch/attach ownership, context cleanup and isolated callbacks.
  External-consumer fixtures handle partial outcomes and slow subscribers
  without initializing a dashboard or blocking collection.

**Phase gate:** T2/T4 and W1 precede automated baseline enforcement; T3
supplies its calibration evidence. W2/W3 prove a complete CI workflow.
The same lifecycle and result contracts must serve CLI and SDK users.
**Gate status:** shared contracts and candidate CI evidence are implemented.
W3 still requires published-pin installation; source-pinned calibration is
historical evidence, not a new release-wide accuracy guarantee.

### Phase 4 — Pilot an optional local Windows service (P3)

- [x] **L1. Define the service contract and ownership model.** Start with
  an explicitly enabled per-user agent backed by W5. Define session IDs,
  start/status/events/export/cancel operations, concurrent-session limits,
  authentication, and error states. Default service attachment to observation;
  make launch, pause, and kill separate capabilities with explicit local
  authorization. Keep an unattended Windows service account deployment a
  later, separately validated option.
  **Done when:** a documented client can monitor a permitted local target
  through authenticated local IPC without dashboard dependencies, and
  unauthorized clients cannot inspect or control another user's sessions.
  **Status:** [#39 / PR #111](https://github.com/JunaidAhamed-7777/DrainTracer/pull/111)
  and the [local agent contract](docs/local-agent.md) define foreground,
  per-user authenticated Windows IPC, private authority and separate launch/
  pause/kill capabilities. Attach is observation-only; this installs no
  unattended service account or network listener.

- [x] **L2. Implement bounded scheduling and failure recovery.** Define
  session deadlines, queue/concurrency limits, restart policy, persistent
  state, report retention, cancellation, and idempotent requests. Handle
  client disconnects, agent upgrades, denied permissions, and disk exhaustion.
  Revalidate PID plus creation time after restart; never automatically
  reattach to a reused PID or retry a launch without its declared policy.
  **Done when:** crash/restart and disconnect drills produce explicit final
  or interrupted states, one failed workload cannot stop other sessions,
  and ordinary service shutdown leaves attached targets running and resumed.
  **Status:** [#40 / PR #112](https://github.com/JunaidAhamed-7777/DrainTracer/pull/112)
  supplies [bounded scheduling/recovery](docs/agent-recovery.md), admission
  journaling, idempotent retry semantics and partial checkpoint recovery.
  Restarts mark old activity interrupted rather than relaunching or adopting
  a saved PID. Native crash/disconnect and ordinary cleanup are exercised.

- [x] **L3. Provide a minimal local client and agent diagnostics.** Reuse
  the CLI and accessible reports for session listing, progress, cancellation,
  and evidence retrieval before adding another dashboard. Expose readiness,
  sampling freshness, queue depth, storage use, and a redacted support bundle.
  Document installation, startup, stop, upgrade, rollback, and removal.
  **Done when:** a developer can diagnose an unhealthy agent, recover a
  report, and remove the service without orphaned owned processes or losing
  unrelated application data.
  **Status:** [#45 / PR #122](https://github.com/JunaidAhamed-7777/DrainTracer/pull/122)
  and the [operations guide](docs/agent-operations.md) deliver live/offline
  diagnostics, controlled recovery, stopped-only removal and previewable
  redacted support bundles. Startup/removal fencing preserves unrelated
  evidence and never substitutes an unverified PID kill.

**Phase gate:** D1, T5, W1, and W5 are prerequisites. Pilot on controlled
development machines first. Network exposure and shared hosting require
the separate privacy and access-control work below.
**Gate status:** per-user foreground-agent functionality is verified; T5's
broader performance gate and human pilot acceptance remain open. No unattended
Windows service-account deployment is implied by the local IPC exercises.

### Phase 5 — Add optional team evidence sharing (P4)

- [x] **S1. Make report privacy an explicit export choice.** Provide local
  preview and redaction profiles covering paths, process/host identifiers,
  arguments, environment metadata, settings, events, and any captured logs.
  Apply the same transformation to JSON and embedded HTML data. Require
  explicit upload opt-in, document exactly what leaves the machine, and
  preserve a useful offline workflow without registration or telemetry.
  **Done when:** seeded secrets and identifying values are absent from all
  redacted outputs and uploads, and users can inspect the result locally.
  **Status:** [#41 / PR #105](https://github.com/JunaidAhamed-7777/DrainTracer/pull/105)
  provides typed share/minimal allowlists, local preview and matched JSON/HTML
  redaction. [Privacy guidance](docs/privacy.md) states what remains and why
  transformed numeric evidence is not guaranteed anonymous. Unknown fields,
  raw paths/errors and seeded identifying values are dropped.

- [ ] **S2. Build the smallest useful team service.** Begin with report
  ingestion, projects, run history, approved baselines, and accessible
  comparison pages keyed to repository, commit, and workload. Keep target
  execution on the developer's machine or organization-owned runner; the
  hosted component stores and analyzes evidence. Use authenticated encrypted
  transport and a bounded local retry queue with backoff and deduplication.
  **Done when:** two teammates can compare a CI run with an approved baseline,
  retries create one logical run, and a server outage does not interrupt
  local monitoring, report export, or target cleanup.
  **Status:** the CLI-first controlled TLS scope is prepared in
  [#48 / PR #141](https://github.com/JunaidAhamed-7777/DrainTracer/pull/141),
  pending its integration. It adds project-bound clients, a private bounded
  retry queue and literal comparison receipts keyed to repository/commit/
  workload. W4 computes the private comparison locally; the service stores
  an explicitly client-computed claim and does not attest or independently
  recompute private evidence. Browser comparison pages, hosted private-
  evidence analysis and a hosted availability promise are not implemented.
  This broader item remains unchecked; no future service release is claimed
  published, and technical credentials do not constitute a human pilot.

- [x] **S3. Establish access control and data lifecycle before sharing.**
  Threat-model uploads, stored reports, rendered HTML, API access, and
  project boundaries. Add project roles, scoped/revocable credentials,
  audit history, quotas, retention, deletion, and export. Isolate untrusted
  report rendering from the authenticated application and validate ingestion
  through W1; offer identity federation when pilot needs justify it.
  **Done when:** tests prove project/tenant isolation, credential revocation,
  safe rendering, quota enforcement, and deletion under a published backup
  expiry policy. No anonymous public report links are enabled by default.
  **Status:** [#46 / PR #125](https://github.com/JunaidAhamed-7777/DrainTracer/pull/125)
  implements [private team authority](docs/team-access.md), transactional
  project/role/revocation checks, bounded sanitized ingestion, quotas,
  retention, deletion, logical evidence backups and fail-closed schema/
  accounting validation. Credentials/deletion authority never roll backward
  with evidence restore; uploaded HTML is not rendered in an authenticated
  service. Identity federation remains conditional on demonstrated need.

- [ ] **S4. Add integrations that shorten investigation.** Start with the
  version-control/CI system used by pilot teams. Link regressions to source
  commits and owners; provide opt-in notifications with deduplication,
  resolution updates, and controls for noise. Add generic outbound events
  only with signing, retry limits, and delivery diagnostics.
  **Done when:** a meaningful new or worsening signal reaches the configured
  destination once, links to authorized evidence, and never exposes private
  report contents through a notification or secret-bearing URL.
  **Status:** [#51](https://github.com/JunaidAhamed-7777/DrainTracer/issues/51)
  remains open pending pilot integration choices and a full opt-in delivery/
  privacy design. No notifications or source-owner routing are claimed.

**Phase gate:** W1/W4 and S1/S3 precede shared project access. S2 can be
developed privately alongside those controls. Before onboarding external
pilot teams, complete a minimum O1 operating baseline: a named incident and
support owner, a recovery runbook, and a successful restore exercise. Agree
O2 pilot acceptance targets before collecting pilot results; mature the
full objectives in Phase 6. Keep remote arbitrary-command execution outside
this first service scope; revisit only with a separate design, demand, and
security review.
**Gate status:** local privacy/authority and the minimum O1 technical
reference are implemented. Prepared PR #141 remains an integration gate for
its controlled TLS slice. Each actual deployment must recheck current
ownership, permissions and restore evidence; neither that fixture nor a
future release substitutes for O2 consent or hosted operating results.

### Phase 6 — Prove service reliability and guide further investment (P5)

- [ ] **O1. Set measurable service objectives and operating procedures.**
  Use pilot measurements to define sampling freshness, completed-run rate,
  report durability, upload latency, availability, and resource budgets.
  Instrument failures without collecting target secrets. Assign incident
  and support ownership; write runbooks for storage exhaustion, agent/server
  incompatibility, outages, migrations, and rollback. Exercise backups and
  restores, including retention/deletion behavior and recovery objectives.
  **Done when:** operators can detect and recover from injected failures,
  verify restored evidence and access controls, and publish realistic support
  expectations before offering production commitments.
  **Status:** [#47 / PR #126](https://github.com/JunaidAhamed-7777/DrainTracer/pull/126)
  completes a minimum [synthetic local operating reference](docs/service-operations.md):
  actual readiness/freshness, explicit completion/read counts, durability,
  latency/resource samples, injected failures and restored evidence/access/
  retention checks. A deployment must separately name its current operator,
  contact, configuration/runbook and recent successful restore exercise.
  Pilot-derived long-term objectives, staffed response, production availability
  and consent are not established; the broader item remains unchecked.

- [ ] **O2. Run a design-partner pilot and measure task success.** Recruit
  developers with repeatable Windows workloads and review installation
  success, time to first useful report, coverage, false alarms, missed known
  growth, investigation time, cleanup/export failures, and operating cost.
  Set numeric acceptance targets before evaluating the pilot; collect
  feedback and optional diagnostics with consent. Validate pricing or
  service quotas against measured cost if paid hosting is pursued.
  **Done when:** teams repeatedly complete local, CI, and shared-comparison
  workflows, can explain the evidence, and agree which failures must be
  fixed before wider availability.
  **Start earlier with the local tool:** the product assessment proposes a
  pilot of 5–10 Windows developers with real long-running workers, desktop
  apps, or multiprocess tools, investigating their own problems with
  minimal coaching. Record whether they identify the contributing process,
  understand the evidence, export a useful report, and verify a change;
  what they would have used otherwise; time to useful evidence; misleading
  signals and missed known problems; and whether they return unprompted.
  This needs no hosted service and should guide I1–I6 before more panels
  are added. The pilot size is a proposal, not evidence of demand.
  **Status:** [#49](https://github.com/JunaidAhamed-7777/DrainTracer/issues/49)
  remains open. Calibration, operating exercises and two technical credentials
  are not recruited participants, consent, uncoached task success or demand.

- [x] **O3. Improve attribution through separately scoped diagnostics.**
  Extend per-process history with PID/creation-time identity, lifetime, and
  growth contribution so new-worker accumulation can be distinguished from
  growth in a persistent worker. Evaluate optional runtime/native tracing
  adapters only where they answer a demonstrated investigation need.
  Record privilege requirements, overhead, privacy impact, and blind spots.
  **Done when:** a multi-process fixture identifies the growing child across
  churn/PID reuse, and any deeper tracing is opt-in with measured cost and
  no claim that aggregate counters locate allocations.
  **Status:** [#52 / PR #128](https://github.com/JunaidAhamed-7777/DrainTracer/pull/128)
  records 18 representative repeated accumulation/within-worker/churn cases
  and the [full aggregate severity decision](docs/worker-growth-policy.md),
  reusing delivered identity/history/contributor evidence. Positive components
  are distinct from net growth; HIGH-policy misses remain visible. The
  [tracing scope decision](docs/deeper-tracing-scope.md) evaluates privilege,
  cost, privacy and blind spots. No adapter is selected absent demonstrated
  need, and no unmeasured adapter cost or human-pilot result is asserted.

- [ ] **O4. Evaluate broader deployment only against demonstrated demand.**
  Scope unattended Windows service accounts, enterprise/private hosting,
  and Linux/macOS support as separate proposals with users and maintainers.
  For each, specify metric meaning, launch/attach/control behavior,
  distribution, permissions, test infrastructure, and ongoing support cost.
  **Done when:** a compatibility prototype and representative workloads
  justify a supported commitment; unsupported platforms remain clearly
  labeled rather than inheriting Windows guarantees.
  **Status:** [#53](https://github.com/JunaidAhamed-7777/DrainTracer/issues/53)
  remains open for broader demand and maintainability evidence. Linux core
  support is implemented in v5.4.0 development source, an unpublished
  candidate; WSL measurements do not justify
  arbitrary native-Linux deployments, macOS, enterprise hosting or unattended
  Windows service-account guarantees.

### How to turn this checklist into delivered work

1. Preserve the measured T5 failures and complete W3's published-package
   check before claiming their gates passed. Keep S2's broader pages/hosted
   analysis and O1's pilot-derived objectives open. Prioritize the five
   original human/demand workstreams and the ten bounded starters above;
   do not substitute technical fixtures for first-time users or consent.
2. Before implementation, give each selected item a maintainer/owner, scope,
   dependency links, expected user outcome, acceptance checks, and a size
   estimate. Split tasks that cannot be reviewed independently into smaller
   linked issues; record design decisions when contracts or heuristics change.
3. Deliver the CLI/CI path before expanding service scope. A useful first
   service slice is **local collection → validated, redacted report → private
   project history → approved-baseline comparison**. Keep authentication,
   isolation, retention, and recovery in that slice's completion criteria.
4. Close an item only with linked implementation, tests or recorded manual
   verification, updated usage/troubleshooting docs, and evidence for its
   stated completion criteria. Apply the repository's SemVer policy when
   publishing a release; commits, pushes, and merges do not automatically
   advance the version. Do not assign speculative release versions here.
5. Review priorities after each phase and pilot. Favor fixes that reduce
   inconclusive checks, false alarms, failed cleanup, and setup friction over
   adding integrations without demonstrated use. Keep deferred items
   unchecked and explain changes to scope instead of treating this plan as a
   fixed delivery promise.
