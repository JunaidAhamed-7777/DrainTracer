# Controlled local baselines

`draintracer-baseline` stores deliberately approved repeated runs locally. It
does not fetch reports, execute workloads, promote automatically, or identify a
remote reviewer. Treat the store as private report data and protect its directory
with ordinary filesystem access controls. The declared approver is an audit
claim; enforce real review authority through your repository/access policy.

Create or replace a baseline with `promote STORE --run A.json --run B.json
--approved-by NAME --reason TEXT --source-commit FULL_SHA`. A promotion requires
2–20 distinct schema-3 launch reports, a declared workload, measured drift for every
resource, explicit judged/full coverage, and equivalent target, arguments,
settings, detector version and environment. A new baseline is compared with the
existing one before replacement. Incompatible or insufficient reports cannot be
promoted, even with a suppression. Recorded startup, cleanup, target, monitor,
interruption or workload failures make evidence unusable regardless of measured
coverage. A raw workload record requires completed steps and confirmed exercise
cleanup. Timing-quality fields introduced by the bounded sampling work are a
prerequisite: clock adjustments, sampling gaps and paused samples must each be
known zero. Missing legacy counts require recollection; neither approval nor a
suppression can waive continuity loss. A measured regression, any recorded leak signal, or exceeded budget
also requires `--accept-regression`, and remains recorded in the audit.

The store copies validated reports under their SHA-256 content identifiers.
Programmatic promote/check calls first detach and revalidate current report
data and recompute summaries; editing a LoadedReport dictionary after loading
cannot reuse a stale clean summary or bypass an initial-signal approval gate.
Checks read these copies and verify their hashes; editing or replacing an
original input file cannot silently change the approved baseline. The atomic
manifest records every promotion/suppression in sequence, including the OS
actor, separately declared approver, source commit and reason. Audit entries
have chained hashes to detect accidental alteration, not to authenticate a
writer who can replace the whole directory. Writers use an OS-released lock;
failed manifest replacement preserves the previous baseline. Unreferenced
content files after a failed write are harmless and are never selected as a
baseline automatically. Evidence objects are also written to private temporary
files, flushed and atomically published, so a writer crash never leaves a
partial object at an approved content name. A retry can proceed while an
unreferenced temporary file remains. History stops at 512 entries rather than
truncating.

`check STORE --run NEW_A.json --run NEW_B.json --source-commit FULL_SHA` reuses
strict repeated-run statistics. Report-sourced peak budgets must agree in all
approved baseline runs; explicit `--budget-private`, `--budget-handles`, and
`--budget-threads` replace only the supplied metrics. Unknown peaks and
inconclusive overlapping ranges cannot pass a controlled check. An approved
baseline is a policy choice, not proof that a workload is free of leaks. The sole
permitted informational caveat is the engine-generated `clean recipe platform
basics` with no blocking verdict. It records omitted OS location values for an
explicitly clean recipe; selected/inherited environment, incomplete platform
metadata and every other caveat remain gates.

An exceptional `suppress STORE --resource handles --kind regression --expires
FUTURE_UTC_TIME --approved-by NAME --reason TEXT --source-commit FULL_SHA`
records a narrow exception (replace `FUTURE_UTC_TIME` with a UTC timestamp
ending in `Z`). Expiry must be in the future and at most 90 days
away. Suppressions apply only to one resource and finding kind (`regression` or
`budget`), the exact baseline revision and its workload. Promotion invalidates
old suppressions. They never suppress target/provenance/coverage gates, missing
metrics, unknown or inconclusive comparisons. The JSON result always includes
the original findings and the applied/expired exceptions with their reasons.

Comparison/audit output is machine JSON on stdout; input and store errors are
JSON errors on stderr. Exit
codes are 0 for a conclusive pass, 1 for an unsuppressed regression, 2 for an
input/store error, 3 for incompatible/insufficient/inconclusive evidence, and 4
for an unsuppressed peak budget. No global detector defaults or calibration
thresholds change. These conservative rules use the existing repeated-run
method; broader detector calibration remains separate work.

`draintracer diff A.json B.json --report-budgets` also opts into reading the
earlier report's budgets. Without that flag, existing diff behavior is unchanged.
When repeated earlier runs disagree about their budgets, the command refuses an
ambiguous policy instead of selecting the first. Explicit flags override only
the named metric. `draintracer-baseline audit STORE` prints the intact review
history and current policy as JSON without changing the approval.

The clean-wheel verification runs four independent real steady targets, approves
the first pair and checks the second. It permits a conclusive pass or explicitly
reported observed overlap, and rejects lost provenance or measurement coverage.
Deterministic regression fixtures separately verify repeatable failure, strict
promotion gates, scoped exceptions, atomic failure preservation and audit/hash
validation. This is functional verification, not broad detector calibration.

Verification on 2026-10-31: the 20 focused baseline tests and all 743 unit tests
passed on Windows (23 platform skips) and WSL Linux (16 skips). The native
mid-object-write crash drill exited the writer abruptly, retained the previous
approval, released its OS lock and completed a subsequent retry. A clean source
distribution and wheel passed strict metadata checks; the wheel's Python members
matched the committed runtime inventory exactly. Clean installation then ran
the four independent workload recordings and the promotion/check/audit commands
outside the checkout. A dedicated user approval system, broad calibration study
and power-loss durability test are not claimed by this local functional check.

Continuity/clean-recipe admission is tested with deterministic counter and caveat
regressions here. Final native recipe promotion/check and installed real-run
verification require the combined timing/workload source; those integrated
checks must pass before publishing this feature.


Native integration verification on 2026-10-31 combined the clean recipe policy,
clock/gap accounting and bounded report writer. A clean candidate wheel produced
eight independent real recipe recordings (40–41 samples each), all with zero
clock adjustments, sampling gaps and paused samples and complete workflow/owned
cleanup. The first two clean recipes were deliberately promoted; the second
pair passed (`unchanged`, exit 0). Selected and inherited environment groups
were each refused for promotion and check, including explicit regression
acceptance, while the approved audit remained unchanged. This verifies the
recorded-choice clean exception; it does not establish complete OS equivalence.
The combined native suite passed 903 tests (25 platform skips). The source and
isolated wheel passed strict metadata checks and exact runtime-file inventory.
`tests/verify_controlled_workload.py` repeats the installed eight-run proof;
ordinary installed baseline runs also receive a two-second startup warm-up.
