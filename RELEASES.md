# Current version: v5.6.0

Release records and development milestones are listed newest first. The
current runtime version here matches `DashboardApp.VERSION` in
[src/dashboard.py](src/dashboard.py). The README shows the release series
as `vMAJOR.MINOR.0`; patch releases stay in that series. A prepared version
entry alone does not establish publication: check its matching release tag
and PyPI distribution.

The v5.4.0 and v5.3.3 entries below record development candidates that were
not separately published to PyPI. Their changes are included in v5.5.0.
Historical entries and source-pinned measurements retain the versions under
which they were prepared or collected.

## v5.6.0 — 2026-11-01

- Added optional foreground sharing through `draintracer-share`: an explicit
  TLS listener and client on an organization-controlled host. The listener
  defaults to loopback; a network bind requires an explicit opt-in.
  Certificate, hostname and protocol verification are mandatory; frames,
  concurrent workers and socket I/O are bounded.
- Kept project roles, revocation, retention and deletion under live authority.
  Credentials come from private files, and each request rechecks the current
  project and role. Reports are shared as JSON transformed by privacy profiles,
  with local preview and a second server-side transformation. Uploaded HTML,
  anonymous public links and remote target execution are not exposed.
- Added a private offline queue with bounded storage, entry counts, expiry,
  attempts and backoff. Stable request and payload identities deduplicate
  retries, including a lost acknowledgement; changed payloads conflict, and
  deletion does not permit resurrection. Outages leave collection, local
  JSON/HTML export and process cleanup independent of sharing.
- Added administrator approval of a validated controlled-baseline revision and
  audit head, plus typed comparison receipts keyed to repository, commit and
  workload. An operator and a separate viewer can retrieve the same stored
  comparison. Receipts remain **client-computed**: private W4 checks run on the
  controlled runner, and unknowns, budget breaches and scoped exceptions keep
  their qualifications. The server does not attest to private measurements.
- Pinned each client to its original project across credential rotation and
  rechecked CA-file write authority, including trusted ancestors, before each
  connection. A same-project rotation refreshes the secret; a different project
  is refused before sending.
- Carried the original flush deadline and entry expiry through credential and
  trust reads, serialization, connection and credential transmission. Each
  flush has a bounded network budget; synchronous local preparation and fsync
  cannot be preempted, so it is not a hard whole-command time limit. A lost
  reply remains uncertain until an explicit retry resolves it.
- Required private passing operating evidence from the current version, no
  older than seven days, before listener admission. It must include actual
  TLS read/upload and queue-recovery observations plus a named operator and
  support contact; local-only evidence cannot admit sharing. This is an
  operator-controlled readiness gate, not signed measurement attestation.
- Added reproducible archive, sdist, isolated-wheel and installed sharing
  workflow helpers. Historical candidate runs and failed attempts retain
  their original source/version identities; they do not substitute for new
  current-version release evidence.

The preceding v5.5.0 release was published by the
[tagged workflow](https://github.com/JunaidAhamed-7777/DrainTracer/actions/runs/41300739840)
from `7b6989ea83d1ab551b63faaaf7c07c7293ea846e`. Fresh official PyPI
installations, CLI/API exports and unchanged default-budget operating checks
passed on Windows and WSL. The actual
[published-package example](https://github.com/JunaidAhamed-7777/DrainTracer/actions/runs/41301570764)
installed exact 5.5.0 with empty candidate-wheel inputs: growth returned 1,
steady returned 0, strict comparison was comparable/improved with memory
resolved, and the deliberate short run returned 3. All three retained report
sets recorded complete cleanup with zero remaining processes. This closed
#44; the preceding entry's publication-time pending statement remains history.

Release verification separates current-version source and installed-workflow
checks from public distribution checks. Tag/PyPI publication, fresh official
PyPI installations and the tagged maintained CI example are postpublication
gates. Record their actual outcomes in the GitHub release notes and CI
receipts after those results exist.

See [sharing](docs/team-sharing.md), [team access](docs/team-access.md) and
[operating procedures](docs/service-operations.md). This release provides an
operator-run TLS listener. Managed hosting, a production SLA, private-source
attestation and human-pilot acceptance are outside its scope. Technical
credentials are not recruited participants or consent.
The earlier candidate operating RSS refusal and later independent pass remain
recorded without an identified cause. Known broader observer timing breaches
remain recorded, and the broader
performance and human/demand goals are unmet. Sharing does not convert those
results into passes.

## v5.5.0 — 2026-11-01

- Added Linux and WSL monitoring alongside Windows, including launch,
  process attachment, recursive process tracking, terminal controls,
  headless checks and offline JSON/HTML exports. Linux private memory is USS
  and handles are open file descriptors; reports record those definitions,
  and strict comparisons reject incompatible counters.
- Hardened process identity and cleanup. Picker selections retain their
  creation time, rejected descendants remain rejected, rate baselines restart
  on identity changes, and controls refuse unreadable or changed identities.
  Partial pause rollback retains owed resumes; ordinary detach preserves
  attached applications and pre-existing stops.
- Made incomplete evidence explicit across the dashboard, machine output,
  HTML and comparisons. Unknown counters remain unknown, unjudged windows
  remain collecting/inconclusive, and exact peaks, strongest signals and
  event-eviction counts survive chart thinning or queue overflow. Structured
  outcomes distinguish startup, target, measurement, monitor and cleanup
  failures; optional policies require full coverage or conclusive evidence.
- Added opt-in atomic checkpoints and qualified interrupted-run recovery.
  Recovered prefixes state their cutoff and unconfirmed tail; recovery never
  grants authority to restart or control a saved process identity. Dense
  exports stay within the public reader's bounds while retaining exact facts.
- Added safe setup diagnostics and configuration previews, explicit no-color,
  plain and quiet output, visible configured budgets, process-scoped timeline
  ranges and named markers, sampled host pressure with age, and a saved-report
  comparison view that leaves monitoring running.
- Made offline HTML evidence usable without chart interaction: semantic
  evidence/sample tables, CSV downloads, labeled range and Zoom controls,
  visible focus, text qualifications and tested light/dark contrast. Recorded
  browser and accessibility-interface checks have explicit client and native
  keyboard coverage limits.
- Published bounded report and summary contracts with a standalone validator,
  a supported typed Python session API with immutable snapshots and isolated
  callbacks, and a redacted investigation summary with optional profiler
  handoff guidance. Privacy profiles provide a local preview and apply the
  same typed allowlist to JSON and HTML; transformed numerical evidence is
  not guaranteed anonymous.
- Added bounded workload recipes with explicit interpreter, working directory
  and environment choices, readiness checks, exercise steps and separate
  opt-in redacted log capture. Workload failures remain distinct from leak
  signals. Secret selected/inherited environment values are never recorded;
  their unknown equivalence blocks strict comparisons.
- Added repeated-run comparisons and deliberate controlled-baseline approval,
  with recorded actor/reason/source, per-resource overlap/unknown refusal,
  resource budgets and scoped expiring exceptions. Missing provenance or
  interrupted sampling cannot be waived into a pass. Process contribution
  rankings remain independent for each run and describe positive growth,
  including offsets from exits/decreases, rather than allocation cause.
- Added an opt-in per-user foreground Windows agent with authenticated local
  IPC, bounded admission, explicit capabilities, durable session recovery,
  diagnostics, previewable support bundles and stopped-only removal.
  It installs no unattended service account or network listener. Added local
  private project storage with transactional roles, revocation, sanitized
  ingestion, quotas, retention, deletion and authority-preserving logical
  evidence restore; this local core exposes no hosted sharing endpoint.
- Added technically checked install/measure/change/compare, separate-Python,
  native/batch and dedicated disposable-service observation guides. Service
  verification checks same-identity survival after detach and guarded fixture
  cleanup. These command checks are separate from first-time user acceptance.
- Expanded protected PR/release verification across Windows 2022/2025 and
  Python 3.10/3.14 plus Linux, installed-wheel checks and bounded failure
  artifacts. Added a pinned Windows workload action/template and an explicit
  published-package verification mode with unchanged strict outcome and
  artifact gates.

Validation and remaining limits:

- Recorded source-pinned calibration includes 72 repeated Windows/WSL
  runs with benign startup/cache warnings and slow-growth HIGH-policy misses
  preserved. The separate worker-policy study retains all 18 runs and keeps
  aggregate severity; no detection thresholds were tuned to hide misses.
- Observer reference/verification includes repeated tree sizes, failure drills
  and one 30-minute 31-process soak per OS. Both verification sets retain
  `budget_failures`: seven timing-ceiling breaches across five of twenty
  cases. Functional evidence, reader bounds, identity/cleanup checks and the
  declared CPU/RSS ceilings passed. An unexpected Windows thread-growth
  signal remains recorded with its origin unknown. The separate host-call
  component proof passed its frozen ceilings; it does not make the broader
  T5 performance gate pass. See
  [observer verification](docs/observer-verification-results.md) and
  [host component results](docs/host-budget-results.md).
- The studies above identify their actual v5.4.0 development source and
  environment. They are not relabeled as v5.5.0 measurements or universal
  throughput, accuracy or cleanup guarantees. Physical sleep/wake, forced
  kernel PID reuse and a consented human pilot were not established.
- The unpublished v5.5.0 candidate
  (`5b93f13c2f677cfab39a3ff7b046185d973ccc69`) returned native
  combined-verifier Ops readiness false; its gate is unknown because temporary
  evidence was deleted. A separate persistent trial passed all 66 functional
  probes, timing budgets, rates and final cleanup but exceeded the unchanged
  268,435,456-byte RSS limit (driver 400,912,384; agent 402,669,568). A later
  independent trial of the same wheel passed at 47,386,624/50,384,896 bytes.
  These used the predeclared 10-second helper readiness protocol, separate
  from default 2-second/PyPI verification. The earlier RSS cause remains
  unknown. Recorded passes do not establish a universal resource bound or
  explain earlier failures.
- The first-time contributor, human pilot, demand-led diagnostics,
  notifications and broader deployment work remain open in issues
  [#31](https://github.com/JunaidAhamed-7777/DrainTracer/issues/31),
  [#49](https://github.com/JunaidAhamed-7777/DrainTracer/issues/49),
  [#50](https://github.com/JunaidAhamed-7777/DrainTracer/issues/50),
  [#51](https://github.com/JunaidAhamed-7777/DrainTracer/issues/51) and
  [#53](https://github.com/JunaidAhamed-7777/DrainTracer/issues/53).
  Synthetic operating exercises establish bounded local observations, not
  participant consent, a production SLA or long-term service availability.
- The maintained CI action's candidate-wheel example passed; its exact
  v5.5.0 PyPI-install check is a post-publication gate. Keep
  [#44](https://github.com/JunaidAhamed-7777/DrainTracer/issues/44) open until
  that pinned published-package run and its retained evidence pass.

## v5.4.0 — 2026-10-29

- Added Linux support, including WSL: executable and Python targets,
  executable shebang scripts, process attachment, immediate dashboard and
  picker controls, pause/resume, headless checks, and JSON/HTML reports.
  Desktop file selection is optional, with typed-path selection when no
  graphical picker is available.
- Measured Linux private memory as USS and handles as open file descriptors,
  with `/proc` descriptor classification and honest unavailable/partial
  coverage. Reports identify platform counter definitions; strict comparisons
  reject incompatible definitions. Windows resource counters retain their
  existing meanings.
- Isolated Linux launch sessions to track reparented descendants, preserved
  pre-existing process stops, and hardened PID reuse, interrupted/partial
  pause cleanup, terminal disconnection, and children started during shutdown.
  Corrected Windows ABI structure declarations for cross-platform imports.
- Added Linux CI on Python 3.10 and 3.14 alongside Windows checks. Pull
  requests run both platforms, and publishing requires their success.
  Extended regression, real pseudo-terminal, process, resource-growth,
  bounded-workload, and distribution verification coverage.
- Updated installation, usage, package metadata, and development documentation
  for Windows and Linux. This backward-compatible feature release advances
  the release series from v5.3.0 to v5.4.0.

## v5.3.3 — 2026-10-28

- Rejected fractional handle and thread budgets in the main CLI, report-diff
  flags, and TOML settings. Positive whole-valued floats remain valid, while
  fractional private-byte sizes and growth-rate thresholds retain their
  behavior.
- Documented whole-count budgets and added regression coverage for the
  shared parser, both command-line entry points, and configuration files.
- Combined the complementary budget regressions from PRs #67 and #68,
  including a configuration check that fractional handle-growth rates
  remain valid alongside whole-valued thread budgets.

## v5.3.2 — 2026-10-26

- Redacted short valid HTTP Basic credentials in experiment labels before
  saving JSON and HTML reports, including padded values and credentials
  followed by sentence punctuation. Ordinary short labels and existing
  Bearer-token handling retain their behavior.
- Added regression coverage for credential formats and persisted report
  exports across workload, build, and note labels.

## v5.3.1 — 2026-10-23

- Allowed up to three seconds for asynchronous Windows forced termination
  and checked process-handle exit state so terminated processes with retained
  handles no longer remain tracked. Failed or access-denied stops retain live
  processes for cleanup, and PID-reuse protection is preserved.
- Made pause/detach tests wait for Windows suspension and resume to become
  observable, and tested dashboard redraw cadence with a deterministic clock
  instead of a timer that could expire during startup.

## v5.3.0 — 2026-10-23

- Added a bounded PyPI update check below the startup version. New stable
  releases show a red availability notice and green pip upgrade command;
  current versions show a blue contribution link. Failed or offline checks
  are shown as unavailable. Headless runs, help, version-only commands, and
  redirected output do not contact PyPI.
- Replaced numbered startup selection in interactive Windows terminals with
  an Up/Down highlight and Spacebar or Enter selection. Navigation help sits
  below the Get Started box; Q and Escape cancel. Redirected input retains
  the numbered prompt.

## v5.2.1 — 2026-10-23

- Replaced the PyPI description's dashboard screenshot with looping GIF
  excerpts showing install-and-launch commands, sustained memory growth,
  and the exported HTML report. Image URLs are public and absolute, and
  captions identify the recorded version and source-only demo workload.
- Published the description update as a patch release; application behavior
  is unchanged.

## v5.2.0 — 2026-10-22

- Kept unknown measurements apart from real zeros. Unreadable counters are
  marked unmeasured, snapshots record per-metric coverage, and gaining or
  losing access restarts detection windows instead of passing for growth.
- Showed evidence states in leak health: collecting, no sustained growth,
  growth detected, partial coverage, and unavailable, with the session worst
  shown separately. Reports separate target, measurement, and monitor-error
  outcomes (report schema 3); older reports still load.
- Named private bytes as the memory metric behind alerts. The memory card
  charts it with RSS (working set) as context, and narrow tables keep it.
- Attributed growth to processes by PID and creation time. Alerts rank the
  processes that grew over the alert window, separate growth from newly
  started workers, and reports keep live and exited process summaries.
- Exported exact session facts (peaks with timestamps, worst severity,
  episode counts, evaluated time, coverage) that survive chart thinning,
  and measured the monitor's own cadence, collection time, and cost.
- Made fix verification defensible: optional `--workload-id`, `--build`,
  `--operations`, and `--note` labels with redacted arguments, qualified
  comparison verdicts, `diff --strict` (exit 3 when runs are not
  comparable), and optional resource budgets for diffs and headless runs.
- Added focused dashboard views on keys `1`-`5`: overview, process
  inspector, signal evidence, timeline with markers (`M`) and a view-only
  freeze (`F`), and monitor diagnostics, with arrow-key navigation.
- Added optional `--fail-on-inconclusive` and `--fail-on-target-error`
  headless policies. Default exit codes and behavior are unchanged.

## v5.1.1 — 2026-10-22

- Published the first PyPI distribution with a dedicated package description
  whose documentation links and images work outside GitHub.
- Added a tested GitHub Actions publishing workflow using PyPI Trusted
  Publishing, with release tags checked against the runtime version.
- Added fresh-environment wheel verification for the installed command,
  bundled assets, and JSON/HTML exports before publishing.

## v5.1.0 — 2026-10-22

- Added pip installation from a checkout or source archive, plus wheel and
  source distribution builds with dependencies declared in package metadata.
- Added the `draintracer` command, `python -m draintracer`, and `--version`,
  while preserving the existing `python src/main.py` workflow.
- Bundled startup artwork, report styles and scripts, and the runtime
  snapshot. Installed packages validate those files and give pip reinstall
  guidance without attempting Git recovery.
- Kept configuration discovery and generated reports relative to the user's
  working directory. Distribution metadata derives its version from the
  canonical runtime value.
- Updated installation and development instructions and added regression
  coverage for installed startup checks and asset loading.

## v5.0.0 — 2026-10-22

- Established the owner-approved `v5.0.0` baseline for release-based
  [Semantic Versioning](https://semver.org/).
- Replaced per-change version increments with one version per published
  release. Compatible fixes advance PATCH, compatible features advance
  MINOR, and MAJOR changes require explicit repository-owner approval.
- Required comparison of the committed runtime snapshot with the actual
  required file structure before and after every change.
- Added the README release-series label and aligned contributor guides,
  agent instructions, and the pull-request checklist with the new policy.

Commits, pushes, and merges do not independently create new versions.
Prepare the runtime version and this release record together; any release
tag must use the exact full version. See [AGENTS.md](AGENTS.md) for the
versioning and verification rules.

## Earlier history

The original two-part versions and their changes remain in
[CHANGELOG.md](CHANGELOG.md) and the repository history. They are historical
records and have not been renumbered.
