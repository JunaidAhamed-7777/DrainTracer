# Controlled local baselines

`draintracer-baseline` stores deliberately approved repeated runs locally. It
does not fetch reports, execute workloads, promote automatically, or identify a
remote reviewer. Treat the store as private report data and protect its directory
with ordinary filesystem access controls. The declared approver is an audit
claim; enforce real review authority through your repository/access policy.

Create or replace a baseline with `promote STORE --run A.json --run B.json
--approved-by NAME --reason TEXT --source-commit FULL_SHA`. A promotion requires
2–20 distinct schema-3 reports, a declared workload, measured drift for every
resource, explicit judged/full coverage, and equivalent target, arguments,
settings, detector version and environment. A new baseline is compared with the
existing one before replacement. Incompatible or insufficient reports cannot be
promoted, even with a suppression. A measured regression or exceeded budget
also requires `--accept-regression`, and remains recorded in the audit.

The store copies validated reports under their SHA-256 content identifiers.
Checks read these copies and verify their hashes; editing or replacing an
original input file cannot silently change the approved baseline. The atomic
manifest records every promotion/suppression in sequence, including the OS
actor, separately declared approver, source commit and reason. Audit entries
have chained hashes to detect accidental alteration, not to authenticate a
writer who can replace the whole directory. Writers use an OS-released lock;
failed manifest replacement preserves the previous baseline. Unreferenced
content files after a failed write are harmless and are never selected as a
baseline automatically. History stops at 512 entries rather than truncating.

`check STORE --run NEW_A.json --run NEW_B.json --source-commit FULL_SHA` reuses
strict repeated-run statistics. Report-sourced peak budgets must agree in all
approved baseline runs; explicit `--budget-private`, `--budget-handles`, and
`--budget-threads` replace only the supplied metrics. Unknown peaks and
inconclusive overlapping ranges cannot pass a controlled check. An approved
baseline is a policy choice, not proof that a workload is free of leaks.

An exceptional `suppress STORE --resource handles --kind regression --expires
2030-01-02T00:00:00Z --approved-by NAME --reason TEXT --source-commit FULL_SHA`
records a narrow exception. Expiry must be in the future and at most 90 days
away. Suppressions apply only to one resource and finding kind (`regression` or
`budget`), the exact baseline revision and its workload. Promotion invalidates
old suppressions. They never suppress target/provenance/coverage gates, missing
metrics, unknown or inconclusive comparisons. The JSON result always includes
the original findings and the applied/expired exceptions with their reasons.

Output is machine JSON on stdout; failures are JSON errors on stderr. Exit
codes are 0 for a conclusive pass, 1 for an unsuppressed regression, 2 for an
input/store error, 3 for incompatible/insufficient/inconclusive evidence, and 4
for an unsuppressed peak budget. No global detector defaults or calibration
thresholds change. These conservative rules use the existing repeated-run
method; broader detector calibration remains separate work.
