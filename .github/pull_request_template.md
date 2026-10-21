## What changes?

Describe the problem and the behavior after this change. Link a related
issue when applicable.

## Verification

List the checks you ran and their results. Explain any checks you could not
run. Include relevant torture cases for process, detection, or report changes
and screenshots for dashboard changes.

## Checklist

- [ ] I followed `AGENTS.md` and reviewed `CONTRIBUTING.md`.
- [ ] I compared the committed `runtime-manifest.json` with required runtime files before and after this change, updating it only for intentional runtime additions, moves, or removals.
- [ ] I ran `python -m unittest discover -s tests -p test_installation.py` before and after this change.
- [ ] I followed release-based SemVer: compatible fixes use PATCH, compatible features use MINOR, and incompatible MAJOR bumps require explicit owner approval. Commits, pushes, and merges alone do not bump versions.
- [ ] For a release, the runtime version and newest `RELEASES.md` entry match exactly, the README series is `MAJOR.MINOR.0`, and any release tag uses the full runtime version.
- [ ] I updated the relevant documentation and preserved existing guides.
- [ ] I ran compilation, relevant tests/smoke checks, editor diagnostics, and `git diff --check`.
- [ ] My diff excludes generated caches, environments, logs, leak reports, and sensitive data.
