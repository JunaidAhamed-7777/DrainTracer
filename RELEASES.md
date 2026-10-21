# Current version: v5.0.0

Published releases are listed newest first. The exact version here matches
`DashboardApp.VERSION` in [src/dashboard.py](src/dashboard.py). The README
shows the release series as `vMAJOR.MINOR.0`; for example, `v5.0.1` still
belongs to series `v5.0.0`.

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
