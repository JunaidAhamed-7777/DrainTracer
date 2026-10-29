# Current version: v5.4.0

Published releases are listed newest first. The exact version here matches
`DashboardApp.VERSION` in [src/dashboard.py](src/dashboard.py). The README
shows the release series as `vMAJOR.MINOR.0`; for example, `v5.0.1` still
belongs to series `v5.0.0`.

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
