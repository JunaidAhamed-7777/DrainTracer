# Recorded measurement aggregates

The calibration JSON files contain all 36 runs per platform collected under
the pre-registered [protocol](../calibration-protocol.md). They preserve the
inputs, exact source/file hashes, settings, environment, per-resource warning
timing, coverage, observer diagnostics, and cleanup outcomes. These are
identity-free aggregates, not generated leak reports: no process IDs, user
paths, hostnames, environment values, or raw target output are included.

Both files were collected from clean source commit
`6d398f7c1e79e6a3608e03d0a7b31e5faccaf226`, whose runtime version was the
`v5.4.0` candidate. Collection ran on 2026-10-31 from approximately 12:29 to
13:14 UTC. Windows and WSL shared a loaded developer machine. Keep these
historical conditions and source identities when interpreting or comparing
the data; a later release version does not change what was measured.

Read the [analysis and limitations](../calibration-results.md) before using
the results to choose a workload duration or profile. Raw JSON/HTML evidence
remains outside version control under the ignored `torture/reports` directory.
