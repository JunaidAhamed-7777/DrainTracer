# Changelog

This log starts with the open source documentation and branding work. Earlier
feature milestones remain in [future-plan.md](future-plan.md), and the complete
commit history records earlier changes. Versions before `v5.0.0` followed
the historical per-change version rule. Starting with `v5.0.0`, versions
follow Semantic Versioning once per published release, with major bumps
reserved for the repository owner. See [RELEASES.md](RELEASES.md) for the
current release and [AGENTS.md](AGENTS.md) for the release policy.

## v5.5.0 — 2026-11-01

### Added

- Linux/WSL launch and attachment, recursive session ownership, terminal
  controls, USS/private-memory and descriptor counters, and offline exports.
  These changes were developed under the unpublished v5.4.0 candidate.
- `doctor` and preview-first `init`; explicit no-color, plain and quiet
  output; configured-budget help; process-scoped timeline ranges and named
  markers; sampled host CPU/memory with age; and a bounded background-loaded
  dashboard comparison view.
- Offline static evidence and retained-sample tables/CSV, keyboard range and
  Zoom controls, semantic labels/focus, and light/dark contrast checks.
- Public versioned report/summary contracts and bounded validation commands;
  a supported typed session API with explicit launch/attach ownership,
  immutable snapshots, context cleanup and isolated subscriber callbacks.
- Typed share/minimal privacy previews for matching JSON/HTML and a redacted
  investigation summary with bounded profiler handoff guidance.
- Explicit workload interpreter/cwd/environment choices, readiness markers,
  bounded exercise steps and opt-in separately stored redacted logs.
- Conservative comparisons of repeated runs, controlled baseline approval,
  source/actor/reason audit, report/flag budget precedence and scoped expiring
  exceptions. Every resource's overlapping/unknown evidence stays
  inconclusive, even beside a suppressed regression.
- Independent per-side lifetime and retained-alert contributor rankings.
  Positive-growth shares and capacity flags do not pair identities between
  runs, change the aggregate verdict or establish allocation cause.
- A foreground per-user Windows agent using authenticated local IPC, bounded
  scheduling, durable interrupted-session recovery, diagnostics, redacted
  support-bundle previews and removal/startup fencing.
- Optional OS-private local team storage/CLI with project roles, revocation,
  server-side privacy transforms, exact quota accounting, retention/deletion,
  bounded audit history and logical backups that cannot restore revoked
  authority. This release scope starts no sharing listener or hosted pages.
- A bounded local operating/recovery exercise and runbooks. Evidence retains
  failed checks, positive observation denominators and deployment budgets;
  synthetic technical credentials are not a human pilot or an SLA.
- Technically checked Windows walkthrough and separate-Python, native/batch
  and disposable-service recipes, with explicit permissions and cleanup.
- Windows 2022/2025 and minimum/current Python/dependency checks, Linux
  verification, isolated distribution checks and bounded failure artifacts.
  A pinned CI workload action supports candidate wheels and an explicit
  exact-version PyPI verification mode.

### Fixed

- Preserved chosen PID/creation-time identities throughout attachment,
  descendant tracking and controls; refused unverified identities and retained
  owed resumes across partial pause rollback. Restarted CPU/I/O baselines on
  identity changes and reused available per-process history slots.
- Preserved producer-owned worst signals/peaks through queue eviction and
  exact event eviction counts through retention; separated frozen-out arrivals
  from discarded earlier timeline samples.
- Kept unknown or partial totals distinct from zero across TUI/HTML and strict
  comparisons; compared exact window values and qualified one-sided argument
  provenance, retained-only peaks and lost episode detail.
- Prevented target controls while help is open, kept filtered live selection
  distinct from exit, rendered literal process labels safely, and fell back
  from invalid UTF-8 title artwork.
- Recorded startup/cleanup and companion-export failures in structured
  outcomes. Bounded checkpoint/export data without discarding exact facts.
  Clock adjustments, sampling gaps and pauses restart evidence windows and
  remain visible to strict baseline admission.
- Rejected fractional whole-count budgets consistently, including the
  complementary fixes developed under the unpublished v5.3.3 candidate.
  Headless progress includes lifecycle counts and unjudged signal wording.
- Hardened local authority schema/accounting validation, private paths and
  credential lifecycle. Process controls, recovery and removal never acquire
  authority merely from a saved PID or cached permission observation.

- After CI returned policy exit `1` (triggering resource unknown), PR #148
  removes the checkpoint test's unconditional exit-`0` assumption. It
  separately requires a valid run-bound checkpoint, successful
  target/workflow/export, no monitor errors and complete cleanup. Detector
  thresholds, fixture timing and policy behavior remain unchanged.

### Measurement and acceptance status

- Retained all 72 calibration and 18 worker-policy runs, including benign
  warnings, delays, coverage limitations and HIGH-policy misses; thresholds
  remain unchanged.
- Retained the observer verification's two `budget_failures` results and
  seven timing breaches, all failure-drill provenance and the unexplained
  Windows thread-growth signal. CPU/RSS and functional/owned-cleanup checks
  passed; broader T5 timing acceptance remains unmet. Separate source-pinned
  host-call checks passed their own frozen ceilings.
- Recorded candidate operating-check failures and later independent passes
  without changing budgets; earlier RSS cause remains unknown. Helper trials
  are separate from default-budget PyPI verification.
- Historical source versions and Windows/WSL environments remain attached to
  measurements. No universal accuracy/performance claim, production SLA,
  allocation-leak proof or consented human pilot follows from these fixtures.
- Uncoached first-time acceptance and pilot/demand-led work remain open in
  #31/#49/#50/#51/#53. The #44 exact published-package check remains pending
  until its post-PyPI run passes; candidate-wheel validation is separate.
  Broader sharing pages and hosted private-evidence analysis are not part of
  this release's local core.

See [RELEASES.md](RELEASES.md) for release details and
[future-plan.md](future-plan.md) for the unchanged broader acceptance gates.

## v5.3.2 — 2026-10-26

- Fixed short HTTP Basic credentials being retained in experiment labels
  in exported JSON and HTML. Added regression tests for padded values,
  punctuation, ordinary text, and persisted exports. See
  [RELEASES.md](RELEASES.md) for the release record.

## v5.2.1 — 2026-10-23

- Replaced the PyPI description's dashboard screenshot with GIF excerpts
  for install-and-launch commands, live growth detection, and HTML reports.
  Captions clarify the recorded version and the source-only demo workload.

## v5.2.0 — 2026-10-22

### Added

- Added focused dashboard views on number keys, with `Esc` back to the
  overview: `2` a process inspector (searchable tree or growth ranking over a
  window or lifetime, exited processes, and a selected-process detail that
  follows PID and creation time), `3` signal evidence (each window's points,
  slope, monotonic and drop ratios, heuristic score, and verdict, emitted by
  the detector), `4` a timeline (one chart, time-bucketed with stated
  decimation, annotated with lifecycle, pause, access, signal, and marker
  events, and a view-only `F` freeze), and `5` monitor diagnostics. `M` adds
  numbered markers that reports export as `markers`. Arrow, Home/End, and
  paging keys are decoded on Windows and POSIX terminals, help scrolls, and
  legacy code pages get safe replacements for characters they cannot show.
- Made "my fix helped" defensible: reports record an `experiment` section
  (`--workload-id`, `--build`, `--operations`, `--note`, sampling settings,
  and machine details) with target arguments reduced to a count and
  fingerprint and secret-looking label values redacted. Report comparisons
  classify caveats, qualify verdicts that rest on them
  (`IMPROVED (QUALIFIED)`), and `diff --strict` reports `NOT COMPARABLE` or
  `INSUFFICIENT COVERAGE` with exit `3`. Comparisons show whole-run peaks and
  growth per operation, and optional budgets (`--budget-private`,
  `--budget-handles`, `--budget-threads`) fail diffs with exit `4` and
  headless runs with exit `5`, even without a leak signal.
- Measured the monitor itself: snapshots carry cadence, collection time,
  handle-classification age and cost, failed and dropped samples, and
  DrainTracer's own CPU and memory. Leak health shows sample freshness,
  summaries report the monitor cost, and HTML reports add a diagnostics panel.
- Kept exact session facts when chart history is thinned: reports add
  `session_facts` (peaks with timestamps, worst severity and when it was
  reached, signal episode counts, evaluated time, sample counts, coverage)
  that analysis, HTML, and diffs prefer, and mark "chart thinned 1:N".
- Attributed growth to processes: samples carry each process's identity (PID
  and creation time), the detector keeps bounded per-process histories, and
  alerts rank contributors over their window, separating growth from newly
  started workers. Process starts, exits, and restarts are logged, the header
  shows start/exit counts, and reports keep live and exited process summaries.
- Showed the memory metric behind alerts: the memory card and compact strip
  chart private bytes with RSS (working set) as context, narrow process tables
  keep `PRIVATE`, alerts and exported JSON name `private_bytes`, and help
  explains the two counters.
- Added optional `--fail-on-inconclusive` (exit 3) and
  `--fail-on-target-error` (exit 4) headless policies, and separate target,
  measurement, and monitor-error outcomes in summaries and report schema 3.
- Showed evidence states in leak health (collecting, no sustained growth,
  unavailable, partial coverage) and kept the session worst separate.
- Kept unknown measurements apart from real zeros: unreadable counters are
  marked unmeasured, snapshots record per-metric coverage, and access
  changes restart detection windows instead of passing for growth.

### Fixed

- Ended process-inspector growth at the displayed sample, so growth and the
  values beside it describe the same moment.
- Kept the Windows dashboard responsive when a key such as `à` arrives as a
  lone extended-key prefix; it no longer waits for another key press.
- Redacted secret options such as `--password value` pasted into experiment
  labels, not only `password=value` assignments.
- Logged a budget that the first sample already exceeds after the headless
  launch line instead of before it.
- Said "no live process" instead of "no readable process" once the monitored
  tree has exited, so an ended run no longer looks like an access problem.

### Documentation

- Brought the README and working principles up to date with the changes
  above, and reconciled [future-plan.md](future-plan.md) with shipped
  releases so it holds the remaining items from the product assessment.

## v5.1.1 — 2026-10-22

- Fixed the PyPI package description's image and documentation links.
- Added a validated Trusted Publishing workflow and installed-wheel smoke
  checks for the first PyPI upload. See [RELEASES.md](RELEASES.md).

## v5.1.0 — 2026-10-22

- Added pip installation, the `draintracer` command, and wheel/source builds
  with bundled runtime assets and declared dependencies.
- Preserved source-checkout startup and added pip reinstall guidance for
  incomplete installed packages. See [RELEASES.md](RELEASES.md) for details.

## v5.0.0 — 2026-10-22

- Adopted release-based Semantic Versioning with owner-only major bumps.
- Added mandatory snapshot comparisons before and after changes, current
  release records, and a README release-series label. See [RELEASES.md](RELEASES.md).

## v4.8 — 2026-10-21

- Check the runtime snapshot before application imports on every CLI
  launch; missing files offer Enter to pull, restore, and restart.
- Added clear cancellation, noninteractive/headless errors, and missing
  Python dependency instructions before monitoring begins.

## v4.7 — 2026-10-21

- Added opt-in fast-forward Git recovery of missing runtime files, scoped
  restoration, and a bounded restart with the original Python and arguments.

## v4.6 — 2026-10-21

- Added a committed runtime-file snapshot and standard-library-only
  validation for missing installation files and unsafe snapshot paths.

## v4.5 — 2026-10-21

- Escaped Windows device basenames, including extension-bearing names,
  when deriving dated report directories.

## v4.4 — 2026-10-21

- Grouped JSON/HTML export pairs under `leak_reports/reports/` by real
  program filename and creation date, retaining unique UUID filenames.
- Applied the same grouping to custom report roots and kept JSON exports
  available if the reusable HTML frame assets are missing.

## v4.3 — 2026-10-21

- Added Windows-safe report grouping from the real target filename and
  recorded creation date, with validation instead of placeholder names.

## v4.2 — 2026-10-21

- Extracted reusable HTML report styles and chart code into tracked
  `leak_reports/frames/` assets, preserving self-contained offline reports.
- Kept generated and local fixture/demo artifacts ignored.

## v4.1 — 2026-10-21

- Filtered the interactive program list on each typed character and
  Backspace, with Enter selecting a highlighted name match or exact PID.
- Preserved arrow navigation within matches, the 15-row cap, explicit
  refresh/commands, and the non-terminal line-input workflow.

## v4.0 — 2026-10-21

- Added live-query parsing for names, PIDs, and explicit searches while
  keeping incomplete picker commands separate from the active filter.

## v3.9 — 2026-10-21

- Kept keyboard-picker input and notices on single lines so long pasted
  text or an invalid PID cannot hide the prompt in small terminals.

## v3.8 — 2026-10-21

- Enabled Up/Down navigation and Enter selection in the interactive program
  list, with automatic page changes, visible focus, and typed PID/search input.
- Added Escape cancellation and preserved the line-input fallback; arrow
  and PID selection share the same process identity checks.

## v3.7 — 2026-10-21

- Added immediate Windows picker-key decoding for arrows, Enter, text,
  backspace, and Escape, with non-terminal input detection and EOF handling.

## v3.6 — 2026-10-21

- Limited program-list pages to 15 PIDs and added an active-row rendering
  marker for keyboard navigation, retaining smaller pages in short terminals.

## v3.5 — 2026-10-21

- Kept process rows visible in small terminals with compact table/help
  output and single-line search labels.

## v3.4 — 2026-10-21

- Reset supported psutil inventory caches before refreshing so a reused
  PID cannot keep displaying the previous program's name and identity.

## v3.3 — 2026-10-21

- Replaced Hook's manual PID prompt with the searchable program list and
  documented search, paging, refresh, and selection behavior.
- Kept direct CLI/headless commands and safe detach behavior unchanged.

## v3.2 — 2026-10-21

- Added keyboard search, PID selection, paging, refresh, and clear/cancel
  commands to the process picker, including stale-process recovery.

## v3.1 — 2026-10-21

- Added name/PID filtering and bounded process-list pages with literal,
  encoding-safe labels and clear empty-result states.

## v3.0 — 2026-10-21

- Added read-only running-process discovery with sorted names/PIDs and
  creation-time checks for stale or reused selections.

## v2.9 — 2026-10-21

- Connected startup to the Open a program / Hook to a running program
  menu, using the existing file picker and safe PID attachment paths.
- Preserved direct CLI/headless workflows and documented startup choices.

## v2.8 — 2026-10-21

- Added the running-program PID prompt with Task Manager guidance,
  positive-ID validation, cancellation, and detach behavior explained.

## v2.7 — 2026-10-21

- Added the startup choice component for opening a program or hooking to
  a running program, with keyboard validation and cancellation handling.

## v2.6 — 2026-10-21

- Appended a phased roadmap with actionable tasks and completion criteria
  for reliable checks, accessible workflows, developer integrations, and
  optional local and team services.
- Preserved delivered roadmap history; runtime behavior is unchanged.

## v2.5 — 2026-10-21

- Revised the README masthead to show blue water escaping from a crack in a
  straight pipe, matching the compact identity.
- Updated the banner edit brief, accessible description, and license badge color.

## v2.4 — 2026-10-21

- Revised every compact logo, avatar, wordmark, and social card to show a
  straight industrial pipe leaking blue liquid through a visible crack.
- Updated light/dark SVG and PNG variants and the brand palette guidance.

## v2.3 — 2026-10-21

- Rebuilt the README with the new identity, a real dashboard capture, a source
  setup path, concise workflows, and links to the full documentation.
- Added this changelog. Runtime behavior is unchanged by this documentation work.

## v2.2 — 2026-10-21

- Added the illustrated README masthead and its generation brief.
- Documented the visual identity, palette, typography, asset formats, and usage.

## v2.1 — 2026-10-21

- Expanded the existing working-principle reference and added a developer guide.
- Clarified bounded report history, warm-up peaks, and configuration details.
- Preserved delivered roadmap milestones and added discussion topics.

## v2.0 — 2026-10-21

- Introduced the pipe, droplet, and tracing-reticle identity.
- Added editable SVG and PNG marks, light/dark wordmarks, avatars, and a social card.

## v1.9 — 2026-10-21

- Added dedicated usage, configuration, and report/comparison guides.
- Documented process ownership, output handling, headless outcomes, and limitations.

## v1.8 — 2026-10-21

- Added contribution, support, security, and conduct documentation.
- Added GitHub bug/feature forms and a pull request template.
