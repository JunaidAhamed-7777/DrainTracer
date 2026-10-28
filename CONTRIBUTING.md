# Contributing to DrainTracer

DrainTracer welcomes bug reports, documentation fixes, test cases, and code
contributions. Read the [README](README.md) for supported workflows and
[working-principle.md](working-principle.md) for the monitoring design.

## Before you start

Search [existing issues](https://github.com/JunaidAhamed-7777/DrainTracer/issues)
before opening a new one. For a larger feature or a change to leak detection,
open an issue describing the problem, a concrete example, and the behavior
you propose. Small fixes can go straight to a pull request.

Use [SUPPORT.md](SUPPORT.md) for usage questions and
[SECURITY.md](SECURITY.md) for vulnerability reports. Participation follows
the [code of conduct](CODE_OF_CONDUCT.md).

The [legal index](LEGAL.md) links the privacy policy, terms, and dependency
notices. Submit only material you have the right to share. Review report
attachments with the [safe sharing guide](docs/privacy.md); issues, pull
requests, commits, and their author metadata can be public.

## Set up a development checkout

Use Windows 10 or 11 with Python 3.10 or newer. Run these commands from your
checkout's root. Calling the virtual environment directly avoids PowerShell
activation-policy changes.

```powershell
git clone https://github.com/JunaidAhamed-7777/DrainTracer.git
cd DrainTracer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src/main.py --help
```

For a pull request, fork the repository and create a focused branch in your
fork. Runtime dependencies are listed in `requirements.txt`; the unit suite
uses Python's standard-library `unittest`.

## Keep the design intact

Follow [AGENTS.md](AGENTS.md), including its versioning and verification
rules. Keep sampling on the detector thread and terminal rendering on the
main thread. Keep process lifecycle logic separate from the dashboard.

Access-denied processes, a target exiting between queries, and incomplete
cleanup are expected conditions. Do not turn them into uncaught errors or
fabricated metric values. Preserve process identity checks before controlling
a PID. Keep console output readable on legacy Windows code pages and keep
`TITLE.txt` as the startup-art source.

Leak detection requires sustained growth across a qualifying sliding window.
If you change a heuristic, explain its effect on stable workloads, startup
bursts, recovering workloads, and sustained growth. A single sample must not
become evidence of a leak.

## Validate a change

Before and after every change, compare the committed
[`runtime-manifest.json`](runtime-manifest.json) with the current required
runtime file structure. Update its inventory only for intentional runtime
file additions, moves, or removals. Exclude documentation, generated reports,
caches, environments, and user settings. Do not rebuild the snapshot from an
incomplete checkout to accept missing files.

Run the snapshot check before and after the change:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_installation.py
```

Run the following with the virtual environment's Python:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests
git diff --check
git status --short
```

Run relevant real-process cases when changing launch, attach, cleanup,
sampling, reports, or detection. The full suite takes roughly 90 seconds:

```powershell
.\.venv\Scripts\python.exe torture/verify_suite.py
```

For a focused run:

```powershell
.\.venv\Scripts\python.exe torture/verify_suite.py --case memory-growth
```

The [torture guide](torture/README.md) documents the fixtures and their
resource limits. Use workloads you are prepared to have paused or terminated.
For UI changes, check the dashboard in both a wide and a narrow terminal and
inspect its ASCII fallback. Run editor diagnostics on edited files
(`ReadLints` where available), and report unavailable checks honestly.

## Version and documentation

DrainTracer uses Semantic Versioning, `MAJOR.MINOR.PATCH`, beginning with
`v5.0.0`. Read the canonical `DashboardApp.VERSION` in `src/dashboard.py`
before editing. Increment PATCH for backward-compatible fixes and MINOR for
backward-compatible features, resetting PATCH to zero for a new minor release.
A MAJOR bump is reserved for incompatible changes and requires the repository
owner's explicit approval; reset MINOR and PATCH to zero with that bump.

Choose one version for each published release, which may contain many focused
commits. Commits, pushes, and merges do not automatically advance the version.
Coordinate release preparation with the maintainer rather than bumping for
each change.

The runtime version and newest entry in [RELEASES.md](RELEASES.md) must match
exactly. The README shows the release series as `MAJOR.MINOR.0`; patch releases
leave that label unchanged. For example, runtime `v5.0.1` belongs to README
series `v5.0.0`. Update the README label when MAJOR or MINOR changes. Any
release tag must match the full runtime version and release entry.
Keep regenerated reports consistent with
the runtime version, and preserve historical release records.

Update the README and relevant guides when behavior, configuration, or
controls change. Preserve existing reference documents instead of replacing
them with an incomplete summary. Do not commit generated caches, virtual
environments, logs, or leak reports.

Update [PRIVACY.md](PRIVACY.md) and [docs/privacy.md](docs/privacy.md) when
changing network requests, collected fields, logging, export, or retention.
Review [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) when dependencies or
bundled assets change. Describe implemented behavior accurately; a policy
must not promise a privacy control that the code does not provide.

## Open a pull request

Describe the problem, what changes for the user, and the checks you ran.
Include a small reproduction or regression test when it helps demonstrate a
behavioral fix. For terminal layout changes, include screenshots at relevant
sizes. Review your diff for unrelated edits and sensitive paths or data.

Keep commits focused with descriptive messages. Material intentionally
submitted for inclusion is offered under the repository's existing
[MIT license](LICENSE), unless a different compatible arrangement is agreed
before inclusion. Contributors retain their copyright; no copyright
assignment is required. Identify third-party material and preserve its
required notices. See [the contribution terms](TERMS.md#7-contributions-and-public-participation).
Maintainer review and availability determine when a change can be merged;
there is no guaranteed review or release schedule.
