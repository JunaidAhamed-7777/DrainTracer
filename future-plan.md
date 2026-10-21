# Plans for the future

This file preserves the original roadmap and the versions in which those
features landed. Completed work is available today; the discussion topics
at the end are proposals, with no promised release dates. See the
[README](README.md) for the current feature overview and
[CONTRIBUTING.md](CONTRIBUTING.md) to propose or help with a change.

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

Later hardening improves process identity checks, orphan discovery,
bounded report history, error handling, and literal argument forwarding to
Windows batch targets. The current behavior is documented in
[working-principle.md](working-principle.md).

## Topics for discussion

- **Distribution:** evaluate packaging and a documented installed command
  while preserving the straightforward source-checkout workflow.
- **Release verification:** agree on a repeatable Windows release checklist,
  supported Python versions, and how runtime validation should accompany
  published releases.
- **Detector calibration:** collect reproducible, bounded examples of slow
  leaks and legitimate growth to assess defaults and profile guidance.
- **Deeper attribution:** explore which additional diagnostics would help
  connect aggregate tree growth to a specific process or allocation source
  without making routine sampling block or require intrusive instrumentation.

Open an issue with the workload, expected behavior, and proposed scope
before starting a substantial feature. These topics are starting points
for discussion rather than claims that the functionality already exists.

## Proposed delivery plan: a dependable developer service

Planning update: **2026-10-21**. The checklist below extends the discussion
topics into work that can be scoped, implemented, and verified. Every item
is proposed and unchecked; phase names indicate dependency order, not
promised dates or release versions. Keep the delivered milestones above
as history.

The goal is a tool developers can install quickly, use during an
investigation, run repeatedly in CI, and eventually access through an
optional service. Start with reliable local checks and evidence. Build
service capabilities around those same contracts so teams can adopt them
without changing how their applications run.

### Starting point and product boundaries

DrainTracer already supports source-based Windows use, launch and PID
attachment, recursive process tracking, warm-up, profiles, headless checks,
GitHub Actions annotations and summaries, offline HTML/JSON exports, and
report comparison. The proposed work strengthens these features rather
than presenting them as new. There is currently no installed command,
package build configuration, repository CI workflow, public SDK, or hosted
service.

Keep Windows as the supported monitoring platform until another platform
has its own tested process and metric semantics. Keep the offline CLI
useful without an account. Local collection and target execution should
remain on a developer-controlled machine or organization-owned Windows
runner, even if reports are later shared through a team service.

An alert remains evidence of sustained growth, not proof of an allocation
leak. A short, inaccessible, or interrupted run must remain distinguishable
from a completed check with sufficient coverage. Preserve background
sampling, bounded resource use, PID identity checks, and the ownership
distinction between launched and attached targets throughout this plan.

### Phase 1 — Establish trustworthy results and releases (P0)

- [ ] **T1. Automate Windows verification and release checks.** Add a
  repository workflow for compilation, the existing unit suite, report
  script syntax checks, and bounded real-process cases. Define and test
  supported Python/Windows combinations and minimum/current dependencies.
  Separate fast pull request checks from longer soak and release runs;
  retain diagnostic artifacts on failure with a documented expiry.
  **Done when:** a deliberate detector, report, or cleanup regression blocks
  release, and a clean Windows machine can reproduce the checks from the
  documented source commit.

- [ ] **T2. Make automated outcomes explicit.** Define a structured session
  result that separates signal severity, per-resource detection coverage,
  target exit status, monitor/report errors, interruption, and cleanup
  outcome. Add opt-in CI policies for inconclusive checks and failed targets.
  Today an inconclusive headless run can exit `0`; preserve existing exit
  behavior by default and document policy precedence and any migration.
  **Done when:** tests distinguish a warm-up-only run, partial access,
  target exit `23`, interruption, and a signal followed by an export error,
  without automation parsing terminal messages or treating missing data as
  a measured zero.

- [ ] **T3. Calibrate detection against repeatable workloads.** Extend the
  existing bounded fixtures with slow growth, stable caches, repeated
  allocation/recovery, worker churn, and partially inaccessible trees.
  Record workload inputs, machine conditions, settings, valid coverage,
  detection latency, and observed false alarms or missed growth. Test
  repeated runs before changing defaults; explain which profiles fit which
  workloads rather than automatically learning away a leak.
  **Done when:** each proposed threshold change has growth and non-growth
  evidence, a documented minimum useful run length, and known limitations.

- [ ] **T4. Preserve authoritative evidence across long runs.** Keep compact
  run summaries, strongest observed signals, and coverage accounting
  independently of thinned chart history. Expose observed/retained sample
  counts, event truncation, queue drops, and gaps. Distinguish exact summary
  fields from estimates computed from retained samples, using the same
  definitions in JSON, HTML, and comparisons. Evaluate bounded checkpoint
  storage for interrupted sessions.
  **Done when:** a long-run test stays within recorded memory/disk limits,
  preserves its strongest signal after thinning, and produces consistent,
  clearly qualified summaries after partial recovery.

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

**Phase gate:** T1–T5 establish the evidence needed to recommend DrainTracer
as a regression gate. Installation and documentation work can proceed in
parallel; claims about reliable checks must wait for measured results.

### Phase 2 — Make installation and everyday use accessible (P1)

- [ ] **D1. Ship an installable package and console command.** Add package
  metadata, a wheel/source build, and a proposed `draintracer` entry point
  while preserving source execution. Bundle `TITLE.txt` and required report
  resources correctly, and separate the monitor's Python environment from
  the target interpreter when explicitly selected. Publish tested upgrade,
  uninstall, compatibility, and release-checksum instructions.
  **Done when:** a standard user outside the checkout can install a release,
  request help, monitor a bounded sample, export both report formats, and
  uninstall without Git, path editing, or routine elevation.

- [ ] **D2. Add preflight diagnostics and project setup assistance.** Design
  proposed diagnostic, configuration-validation, and project-init commands.
  Check target/interpreter paths, settings precedence, report-directory
  access, permissions, and whether timeout/warm-up leave enough coverage.
  Show resolved settings and actionable fixes before launching; preview
  generated configuration and avoid overwriting existing project files.
  **Done when:** common setup failures can be diagnosed without starting or
  pausing a real application, and generated profiles pass the same validator
  used by normal monitoring.

- [ ] **D3. Provide a guided first useful result and practical recipes.**
  Build a short walkthrough using the included steady and growth fixtures:
  install, run, interpret, export, apply a fix, and compare. Add tested
  recipes for a Python worker, native executable, batch launcher, and an
  already-running Windows service. Each recipe states permissions, workload
  exercise, warm-up, duration, expected outcome, and cleanup behavior.
  **Done when:** developers unfamiliar with the repository can complete the
  walkthrough and explain clear, signal, and inconclusive outcomes without
  maintainer intervention.

- [ ] **D4. Offer accessible terminal presentation and controls.** Extend
  current narrow-terminal and legacy-encoding support with explicit color
  controls, a static/plain mode, and quiet output. Keep verdicts and missing
  readings understandable without color or animation; document keyboard
  controls and destructive actions clearly. Evaluate a view-only pause
  separately from the existing control that pauses the target, with an
  explicit migration if control behavior changes.
  **Done when:** keyboard and screen-reader users can start, inspect,
  interpret, export, and quit a session in plain output and at small terminal
  sizes, with no hidden change to target ownership.

- [ ] **D5. Make exported evidence accessible without chart interaction.**
  Add equivalent textual summaries and data tables/downloads for HTML
  charts, labeled controls, visible focus, keyboard navigation, meaningful
  headings, and non-color signal cues. Verify zoom, contrast, and assistive
  technology behavior while keeping reports self-contained and offline.
  **Done when:** the verdict, coverage, trend, units, and relevant process
  evidence can all be read without hovering, dragging, or seeing the chart.

**Phase gate:** a fresh Windows environment and representative first-time
users complete the documented workflow using the distributed artifact.
Usability and accessibility failures feed back into D1–D5 before promotion.

### Phase 3 — Fit developer workflows and stabilize integrations (P2)

- [ ] **W1. Define stable report and machine-output contracts.** Publish a
  machine-readable report schema and session-summary contract with supported
  versions, optional fields, units, structured errors, and compatibility
  fixtures. Validate untrusted input with size/depth limits; reject unknown
  future schemas clearly and preserve unknown values in older reports.
  Add a streaming format if integrations need live events, keeping machine
  records on stdout and diagnostics on stderr.
  **Done when:** a separate consumer can validate supported reports and
  outcomes without Rich, presentation parsing, or undocumented internal
  imports, and malformed reports fail with useful bounded errors.

- [ ] **W2. Make workload execution reproducible.** Add explicit target
  interpreter, working-directory, and environment selection with redaction
  rules. Support bounded readiness checks and workload exercise steps so a
  server is measured under actual load after it becomes ready. Offer opt-in
  target log capture with separate files, size limits, and secret handling;
  preserve literal argument forwarding and never route target stdin through
  dashboard controls.
  **Done when:** the same recipe runs locally and in CI with recorded
  provenance, readiness failures are distinct from leak signals, and timeouts
  clean up only the processes that session owns.

- [ ] **W3. Supply a maintained Windows CI template and reusable action.**
  Build on existing annotations and job summaries with version-pinned setup,
  strict outcome policies, workload execution, and report retention even on
  failure. Explain runner permissions, costs, time limits, and artifact
  privacy. Keep untrusted pull request jobs away from publishing credentials
  and approved-baseline writes.
  **Done when:** an example pull request displays a useful leak comparison,
  a short inconclusive check fails under the selected policy, and a failing
  job retains enough evidence to reproduce its result.

- [ ] **W4. Turn comparisons into controlled baseline checks.** Extend
  existing `diff` with workload, source commit, settings, detector version,
  environment, and coverage compatibility checks. Use repeated measurements
  and explicit absolute/relative budgets where justified by T3. Require
  deliberate baseline approval; make suppressions scoped, justified, and
  expiring rather than weakening global defaults.
  **Done when:** a known regression fails reproducibly, incompatible or
  insufficient runs cannot silently replace a baseline, and an approved
  update records who changed it and why.

- [ ] **W5. Publish a small supported Python API.** Expose typed session
  options/results, launch/attach, cancellation, snapshots/events, and report
  export through the shared backend. Remove requirements for SDK consumers
  to import dashboard classes or initialize a terminal. Define lifecycle
  ownership, context-managed cleanup, callback isolation/backpressure, and
  compatibility policy before promising a stable interface.
  **Done when:** an external script or test fixture integrates through the
  documented API, handles partial failure, cancels safely, and cannot stall
  sampling with a slow subscriber.

**Phase gate:** T2/T4 and W1 precede automated baseline enforcement; T3
supplies its calibration evidence. W2/W3 prove a complete CI workflow.
The same lifecycle and result contracts must serve CLI and SDK users.

### Phase 4 — Pilot an optional local Windows service (P3)

- [ ] **L1. Define the service contract and ownership model.** Start with
  an explicitly enabled per-user agent backed by W5. Define session IDs,
  start/status/events/export/cancel operations, concurrent-session limits,
  authentication, and error states. Default service attachment to observation;
  make launch, pause, and kill separate capabilities with explicit local
  authorization. Keep an unattended Windows service account deployment a
  later, separately validated option.
  **Done when:** a documented client can monitor a permitted local target
  through authenticated local IPC without dashboard dependencies, and
  unauthorized clients cannot inspect or control another user's sessions.

- [ ] **L2. Implement bounded scheduling and failure recovery.** Define
  session deadlines, queue/concurrency limits, restart policy, persistent
  state, report retention, cancellation, and idempotent requests. Handle
  client disconnects, agent upgrades, denied permissions, and disk exhaustion.
  Revalidate PID plus creation time after restart; never automatically
  reattach to a reused PID or retry a launch without its declared policy.
  **Done when:** crash/restart and disconnect drills produce explicit final
  or interrupted states, one failed workload cannot stop other sessions,
  and ordinary service shutdown leaves attached targets running and resumed.

- [ ] **L3. Provide a minimal local client and agent diagnostics.** Reuse
  the CLI and accessible reports for session listing, progress, cancellation,
  and evidence retrieval before adding another dashboard. Expose readiness,
  sampling freshness, queue depth, storage use, and a redacted support bundle.
  Document installation, startup, stop, upgrade, rollback, and removal.
  **Done when:** a developer can diagnose an unhealthy agent, recover a
  report, and remove the service without orphaned owned processes or losing
  unrelated application data.

**Phase gate:** D1, T5, W1, and W5 are prerequisites. Pilot on controlled
development machines first. Network exposure and shared hosting require
the separate privacy and access-control work below.

### Phase 5 — Add optional team evidence sharing (P4)

- [ ] **S1. Make report privacy an explicit export choice.** Provide local
  preview and redaction profiles covering paths, process/host identifiers,
  arguments, environment metadata, settings, events, and any captured logs.
  Apply the same transformation to JSON and embedded HTML data. Require
  explicit upload opt-in, document exactly what leaves the machine, and
  preserve a useful offline workflow without registration or telemetry.
  **Done when:** seeded secrets and identifying values are absent from all
  redacted outputs and uploads, and users can inspect the result locally.

- [ ] **S2. Build the smallest useful team service.** Begin with report
  ingestion, projects, run history, approved baselines, and accessible
  comparison pages keyed to repository, commit, and workload. Keep target
  execution on the developer's machine or organization-owned runner; the
  hosted component stores and analyzes evidence. Use authenticated encrypted
  transport and a bounded local retry queue with backoff and deduplication.
  **Done when:** two teammates can compare a CI run with an approved baseline,
  retries create one logical run, and a server outage does not interrupt
  local monitoring, report export, or target cleanup.

- [ ] **S3. Establish access control and data lifecycle before sharing.**
  Threat-model uploads, stored reports, rendered HTML, API access, and
  project boundaries. Add project roles, scoped/revocable credentials,
  audit history, quotas, retention, deletion, and export. Isolate untrusted
  report rendering from the authenticated application and validate ingestion
  through W1; offer identity federation when pilot needs justify it.
  **Done when:** tests prove project/tenant isolation, credential revocation,
  safe rendering, quota enforcement, and deletion under a published backup
  expiry policy. No anonymous public report links are enabled by default.

- [ ] **S4. Add integrations that shorten investigation.** Start with the
  version-control/CI system used by pilot teams. Link regressions to source
  commits and owners; provide opt-in notifications with deduplication,
  resolution updates, and controls for noise. Add generic outbound events
  only with signing, retry limits, and delivery diagnostics.
  **Done when:** a meaningful new or worsening signal reaches the configured
  destination once, links to authorized evidence, and never exposes private
  report contents through a notification or secret-bearing URL.

**Phase gate:** W1/W4 and S1/S3 precede shared project access. S2 can be
developed privately alongside those controls. Before onboarding external
pilot teams, complete a minimum O1 operating baseline: a named incident and
support owner, a recovery runbook, and a successful restore exercise. Agree
O2 pilot acceptance targets before collecting pilot results; mature the
full objectives in Phase 6. Keep remote arbitrary-command execution outside
this first service scope; revisit only with a separate design, demand, and
security review.

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

- [ ] **O3. Improve attribution through separately scoped diagnostics.**
  Extend per-process history with PID/creation-time identity, lifetime, and
  growth contribution so new-worker accumulation can be distinguished from
  growth in a persistent worker. Evaluate optional runtime/native tracing
  adapters only where they answer a demonstrated investigation need.
  Record privilege requirements, overhead, privacy impact, and blind spots.
  **Done when:** a multi-process fixture identifies the growing child across
  churn/PID reuse, and any deeper tracing is opt-in with measured cost and
  no claim that aggregate counters locate allocations.

- [ ] **O4. Evaluate broader deployment only against demonstrated demand.**
  Scope unattended Windows service accounts, enterprise/private hosting,
  and Linux/macOS support as separate proposals with users and maintainers.
  For each, specify metric meaning, launch/attach/control behavior,
  distribution, permissions, test infrastructure, and ongoing support cost.
  **Done when:** a compatibility prototype and representative workloads
  justify a supported commitment; unsupported platforms remain clearly
  labeled rather than inheriting Windows guarantees.

### How to turn this checklist into delivered work

1. Start with **T1, T2, and D1** as focused issues; schedule calibration
   (T3) alongside them. Then complete evidence/overhead work (T4/T5) and the
   first-use and accessibility workflow (D2–D5).
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
   stated completion criteria. Apply the repository's version rule to each
   focused change; do not assign speculative release versions here.
5. Review priorities after each phase and pilot. Favor fixes that reduce
   inconclusive checks, false alarms, failed cleanup, and setup friction over
   adding integrations without demonstrated use. Keep deferred items
   unchecked and explain changes to scope instead of treating this plan as a
   fixed delivery promise.
