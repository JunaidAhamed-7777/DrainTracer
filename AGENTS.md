# DrainTracer Agent Instructions

## Project overview

DrainTracer is a Windows and Linux Python 3.10+ CLI/TUI application that
launches a target executable or script, tracks its recursive process tree, samples
resources with `psutil`, and reports potential memory, handle, and thread
leaks through a Rich terminal dashboard.

Core modules are under `src/`:

- `main.py` — CLI entry point, startup title, and file-picker workflow
- `dashboard.py` — Rich UI, controls, event log, and JSON export
- `file_picker.py` — native Windows picker with fallback handling
- `process_runner.py` — process lifecycle and recursive PID tracking
- `leak_detector.py` — background sampling and leak heuristics
- `events.py` — immutable snapshot models and `MetricsStream`

## Mandatory versioning rule

Starting with the owner-approved `v5.0.0` release, use Semantic Versioning:
`vMAJOR.MINOR.PATCH`. Each component is an integer, not a decimal fraction;
for example, `v5.0.99 -> v5.0.100`. Earlier versions retain their history.

- Increment PATCH for backward-compatible bug-fix releases.
- Increment MINOR for backward-compatible feature releases and reset PATCH
  to zero.
- **Only the repository owner may authorize a MAJOR increment.** A major
  release resets MINOR and PATCH to zero. Obtain explicit owner approval
  before publishing incompatible changes or changing the major version.
- Prepare one version increment per published release. Individual commits,
  pushes, and merges do not independently increment the version. Multiple
  small commits can belong to one release; retries and no-op pushes do not
  create another version.

Read `DashboardApp.VERSION` in `src/dashboard.py` before editing. It remains
the canonical runtime value. When preparing a release, update it and the
current version at the top of `RELEASES.md` together. Keep release records
newest first and preserve previous entries. A release tag, when publishing
one, must match the full runtime version; never reuse an existing release
version for different published contents.

Near the top of `README.md`, display the **release series** as
`vMAJOR.MINOR.0`, linking to `RELEASES.md` for the exact current version.
Patch releases keep the same README series; major/minor releases update it.
Keep version values in generated reports consistent when regenerating them.

## Mandatory snapshot check

Before and after every repository change, compare the committed
`runtime-manifest.json` snapshot with the current required runtime file
structure. Run:

```powershell
python -m unittest discover -s tests -p test_installation.py
```

Review any difference. Update the snapshot only for intentional additions,
moves, or removals of required runtime files, in the same focused change.
Do not regenerate it from an incomplete checkout to make missing files pass.
File-content edits alone do not change this structural inventory. Generated
reports, caches, environments, user settings, and documentation such as
`RELEASES.md` are not required runtime files and must stay outside it.

## Implementation guidance

- Preserve the modular backend/UI separation.
- Keep monitoring non-blocking; sampling belongs on the detector background
  thread and UI rendering belongs on the main thread.
- Treat access-denied, terminated-process, missing-file, and partial-cleanup
  states as expected runtime conditions.
- Do not report a leak from a single sample. Preserve the sliding-window and
  monotonic-growth checks unless the detection design is intentionally changed.
- Keep Windows console output safe for legacy code pages.
- Keep `TITLE.txt` as the startup branding source with a readable fallback.
- Update `README.md` when user-visible behavior or controls change.

## Verification and workflow

Before finishing a change:

1. Read the current `DashboardApp.VERSION`; change it only when preparing a
   release under the versioning rule above.
2. Compare the committed snapshot with the current required runtime
   structure before and after the change; run the installation tests.
3. Run `python -m compileall -q src`.
4. Run relevant smoke tests and `ReadLints` on edited files.
5. Run `git diff --check` and confirm the working tree state.
6. Commit focused changes frequently with descriptive messages.

Do not commit generated caches, virtual environments, logs, or leak reports;
the repository `.gitignore` already excludes them.

## Commit authorship

Commits are authored solely by the repository owner. Do not add AI agents or
assistants as co-authors: no `Co-Authored-By:` trailers, "Generated with"
footers, or similar attribution lines in commit messages or pull requests.
