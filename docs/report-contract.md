# Public report and machine-output contracts

DrainTracer reads report schemas **1, 2, and 3**. A report without
`report_schema` is schema 1. Future, zero, fractional, string, and Boolean
schema versions are rejected explicitly. Empty snapshots are valid evidence
for a failed/unfinished session, but cannot be compared by `diff`.

The standalone [report JSON Schema](../schemas/report.json) uses JSON Schema
2020-12. Supported fields are optional unless marked required. Unknown
optional fields remain available to consumers; consumers should ignore
fields they do not recognize. A missing value is unknown, not zero, false,
or success. Schema 1/2 inputs retain this distinction even when they have no
coverage or outcome records. Compatibility examples live under
`tests/fixtures/reports/` and run through the same validator as real reports.

Version 1 is the legacy snapshot format, version 2 adds experiment and
signal evidence, and version 3 adds explicit coverage, per-counter unknowns,
and separated target/measurement/monitor outcomes. Additive optional fields
do not change an existing field's meaning. Incompatible field changes need
a new report schema; consumers must reject unsupported versions.

## Validate and summarize without terminal dependencies

Installed packages provide an independent command:

```powershell
draintracer-report validate .\run.json
draintracer-report summary .\run.json
draintracer-report schema --kind report > report.schema.json
draintracer-report schema --kind summary > summary.schema.json
```

The equivalent module command is `python -m draintracer.report_contract`.
A source checkout uses `python -m src.report_contract`. These commands only
read local files: they never launch/control a target, load Rich, or contact
a server. Validation and schema output also work when site packages are
disabled with `python -S`.

Successful commands emit exactly one JSON document on stdout and exit 0.
An invalid report emits an `error_schema: 1` document on stderr, leaves
stdout empty, and exits 2. Its `error.code` is `invalid_report`; its
`error.message` describes the violated field or bound. CLI syntax errors
use argparse's normal usage diagnostics on stderr. No live streaming
contract is introduced; machine output stays separate from terminal logs.

External Python consumers can use the public, standard-library-only module:

```python
from draintracer.report_contract import read_report, session_summary

report = read_report("run.json")  # raises ReportContractError on invalid input
summary = session_summary(report)
```

`validate_report(decoded_json)` checks an already-decoded JSON object and
returns that object without stripping optional fields. `report_schema()`
and `summary_schema()` return the published schemas as dictionaries.
These names form the supported report-consumer API; they do not expose
monitor lifecycle control.

## Summary version 1

The [session-summary JSON Schema](../schemas/session-summary.json) defines
`summary_schema: 1`. Every listed top-level field is present; unavailable
values are `null`:

- `report_schema`, `report_id`, `version`: source identity, with absent
  legacy version interpreted as schema 1.
- `target`: recorded path, launch/attach mode, and natural target exit code.
- `measurement`: recorded detection, partial-coverage, and result fields.
  Absence of evidence is preserved as `null`.
- `monitor_errors`: structured-outcome error messages, or `null` for old
  inputs that did not record that outcome.
- `peaks`: value, POSIX timestamp in seconds, unit, and provenance for each
  metric. `session_facts` means the recorded whole-session peak;
  `retained_snapshots` means a fallback that may miss thinned observations;
  `unavailable` means there was no readable counter. A measured zero stays
  zero. Paused/failed samples and unavailable per-metric totals are excluded.
- `worst_severity`: strongest recorded severity, or `null` when no severity
  was recorded. It describes a heuristic resource-growth signal, not proof
  of an allocation leak.
- `counts`: retained snapshot count, exact observed count including paused
  and failed observations when available,
  and chart stride when recorded. Retained count is never substituted for
  an unknown whole-session count.

Memory counters use bytes; handles/threads use counts; CPU uses percent;
I/O rates use bytes/second. Memory and handle definitions remain platform
specific: consult `experiment.environment` for Windows private bytes versus
Linux USS, and Windows handles versus Linux descriptors. No unit conversion
can make those definitions interchangeable.

## Untrusted-input bounds

The file reader consumes at most **64 MiB**, rejects nesting deeper than
**64** and more than **1,000,000 JSON tokens** before decoding, and bounds
decoded value count to the same limit. Strings containing brackets do not
increase nesting. Malformed UTF-8/JSON, non-finite numbers, unsupported
versions, duplicate object keys, integers outside finite numeric representability,
and invalid known field shapes fail with bounded useful errors that do not echo
the rejected numeric payload.
`diff` uses this same reader, so it applies the same ingestion policy.
In particular, a numerically unrepresentable value now rejects the report
with exit 2 instead of being skipped as an unknown counter in a comparison.
The reader never assigns a clean or regression verdict to that input.

The public schema describes shape and meaning; byte/token/depth limits are
additional ingestion requirements. No field can instruct the validator to
execute code, follow a URL, or load another file. Summaries preserve paths
and identifiers: use the explicit privacy export workflow before sharing.
