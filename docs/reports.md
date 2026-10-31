# Reports and comparisons

[README](../README.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Report schema](../working-principle.md#7-leak-report-structure)

Reports can contain sensitive paths, process identities, timestamps, and
free text. Review both JSON and the HTML's embedded data before sharing;
label redaction is not whole-report sanitization. See
[Privacy and safe sharing](privacy.md) and the [privacy policy](../PRIVACY.md)
for storage, network behavior, retention, and deletion.

## Export a session

Press `E` in the dashboard to export the samples collected so far. Each
successful export writes a JSON report and an HTML companion with the same
UUID. Reusable generation assets are tracked; generated reports are ignored.
For `worker.exe` monitored on October 21, 2026, the layout is:

```text
leak_reports/
  frames/
    report.css
    report.js
  reports/
    worker.exe_2026-10-21/
      leak_report_3f2a9c1e-5b7d-4e0a-9c61-0d2f7e8b4a10.json
      leak_report_3f2a9c1e-5b7d-4e0a-9c61-0d2f7e8b4a10.html
```

Press `O` to open the latest HTML export. Dashboard exports are explicit;
headless runs automatically export after monitoring ends. Set
`--report-dir DIR` or `report_dir` in a settings file to change the output
root; the program/date folder is created beneath that root. Its date comes
from the report's recorded `created_at` offset, and invalid Windows filename
characters are replaced while long names are bounded. Missing target/date
metadata is rejected instead of making placeholder directory names.
A fresh UUID prevents repeated same-program/day exports from overwriting
earlier reports. Both formats always share the same grouped directory.
The event log and session summary show the actual filenames.

Both formats are written through temporary sibling files followed by a
rename. If HTML rendering or writing fails, the JSON report is retained and
the error is logged and recorded in the JSON outcome through an atomic update.
If that update also fails, the original JSON remains usable and the session
logs the update failure. An in-progress dashboard export can have
`session_ended_at: null` because monitoring has not ended yet.

## Read the HTML report

Reusable styles and chart code are committed in
`leak_reports/frames/report.css` and `leak_reports/frames/report.js`.
They contain generation assets, not sample measurements. The exporter
locates them relative to the application source and embeds them in each
generated page; they are not required alongside a report when sharing it.

Open the `.html` file directly in a browser. It is self-contained: styles,
charts, and report data are embedded, with no external libraries or network
requests. Sharing the file is enough to share its interactive view.

The report includes:

- A verdict and each resource's strongest observed signal, net growth,
  drift, and peak. `INCONCLUSIVE` means no detection window ever filled.
  `PARTIAL` qualifies signal-free results when readings are missing;
  `UNAVAILABLE` identifies resources with no readable live-process samples.
  Observed signal severity remains visible alongside coverage qualifications.
- Memory, handles, handle categories, threads, and CPU charts. Hover for
  values, drag to zoom, and use legend entries to toggle series.
- Markers where signals appeared or escalated, warm-up and pause bands,
  and the time the root exited.
- Continuous signal episodes, events, and the final available process list.
- A Monitor diagnostics panel: requested and achieved cadence, collection
  time, the monitor's own CPU and memory, handle-classification passes and
  age, failed and dropped samples, and how long detection evaluated windows.

Growth is the last measured value minus the first. Drift is a regression
slope over measured samples. With a warm-up, growth, drift, and resource
card peaks use post-warm-up samples. Thinned resource cards say `retained
peak` because their stored samples can miss a short spike; exact whole-run
peaks remain in the Session card and JSON session facts. Charts and the overall RSS/CPU peaks
include startup. Charts break across missing readings rather than
interpreting a paused or failed sample as a measured zero.

The HTML and JSON use the same report dictionary, so their exported samples
agree. Handle classifications are less frequent than the main sampling
interval and use their own timestamps.

## Use the JSON data

The [public report contract](report-contract.md) supplies standalone schemas,
bounded validation, compatibility fixtures, and a versioned machine summary.
Use `draintracer-report validate FILE` or `draintracer-report summary FILE`
for dependency-free local consumption.

The current layout is `report_schema: 3`. The report contains identity and
session timestamps, application version, target and mode, resolved
settings, warm-up status, measurement coverage, separate target,
measurement, and monitor-error outcomes, events, and chronological
telemetry snapshots. Each snapshot contains process records, tree totals,
alerts, severity, per-metric coverage, per-resource evidence states, and
any handle classifications. Counters a process could not be read for are
listed in its `unmeasured` field; their `0` values are unknown, and analysis
and comparisons treat them as gaps. Comparisons warn when either run had
partial coverage.

Process records carry `create_time`; with the PID it identifies one process,
so a reused PID is a different process. Snapshots list `lifecycle` events
(`started`, `exited`, `restarted`) and `process_counts`. Alerts list
`contributors` (PID, creation time, name, `delta`, `share`, `new_process`,
`exited`) ranked over the alert window, with `growth_from_new_processes`,
`growth_within_existing`, and `mostly_new_processes`. The top-level
`processes` section holds the final counts and bounded `live` and `exited`
per-process summaries (first, last, peak, and growth of private bytes,
handles, and threads). Older reports lack these fields.

For integrations, inspect `report_schema` and allow additional fields.
Do not treat a missing field in an older report as an observed zero.
Enum values such as resource and severity are lowercase strings. Local
ISO 8601 timestamps include their UTC offset; sample timestamps are Unix
timestamps. See the [full schema and field descriptions](../working-principle.md#7-leak-report-structure)
for exact record contents.

Snapshots carry `diagnostics`: requested and achieved (`actual_interval_ms`)
cadence, `collection_ms`, handle-classification age and cost, failed and
dropped sample counts, and the monitor's own CPU and memory.

Reports retain bounded history: up to 7,200 snapshots and the most recent
200 event messages. Once full, snapshot resolution is progressively thinned
across the whole session rather than losing the beginning. Important state
changes are preserved up to a bounded allowance; `snapshot_stride` records
the thinning, and the HTML report says "chart thinned 1:N". Long reports are
therefore not guaranteed to contain every sample or every event.

`event_counts` records session event messages: `observed` is the number added
to the event log, `retained` is the number exported, and `dropped` is the
number evicted by its retention limit. The HTML event section shows all three.
These counts describe event messages, independently of chart thinning and
snapshot queue drops. Older reports without this field label retention counts
as unavailable.

The top-level `session_facts` section is computed from every sample, not
the retained ones: `samples` (taken, collected, measured, paused, failed),
`peaks` (value and timestamp per total), `signals` (worst severity and when
it was first reached, episode and escalation counts, and time signalled per
resource), `evaluation` (time after warm-up and time with a judged window),
`monitor` (cadence, collection time, monitor CPU and memory,
classification cost, failed and dropped samples), `coverage`, and `chart`
(stride, stored, and seen snapshots). HTML and comparison summaries use
these exact facts for peaks, worst severities, episode counts, and sample
counts; growth and drift still come from retained snapshots. Older reports
without `session_facts` fall back to retained snapshots, and comparisons
warn when such a report was thinned. The headless failure decision uses the
strongest signal observed during the session.

Exact facts are recorded before snapshots enter the bounded consumer queue.
If that queue overflows while rendering or input is stalled, the chart and
event transitions can lose detail; `monitor.dropped_snapshots` counts those
discarded snapshots, including an overflow on the final publication. Peaks,
worst signals, budget decisions, coverage, and sample counts still include
every collected observation. `chart.seen` counts snapshots accepted into chart
history, so it can be smaller than `samples.taken` after queue loss.

The top-level `markers` list holds the markers added with `M` during a
dashboard session: `index`, `label` (`marker N`), `timestamp`, `time`, and
`elapsed_seconds` since monitoring started. Markers record when the user
noted something; they do not establish cause. Reports without the field
have no markers.

### Startup and cleanup outcomes

`outcome.startup` records `state` (`not_started`, `started`, or `failed`) and
an `error` when startup fails. Headless failed starts attempt to export the
available evidence and still exit `2`; that startup result takes precedence
even if an initial sample contained a signal.

`outcome.cleanup` records `state` (`not_attempted`, `complete`, or
`incomplete`), ownership (`launched` or `attached`), and exact
`attempted_actions` counts for `terminate`, `kill`, and `detach`. Detach
includes resuming processes suspended by DrainTracer. `remaining_count`,
up to 50 `remaining_processes` with PID, creation time, and `resume_required`, `omitted_processes`,
`resume_required`, and up to 20 distinct `errors` describe partial cleanup.
These are observed results, not proof that a PID remains current later.

Cleanup exceptions are monitor errors and headless runs still attempt to
export evidence. A method that returns normally with survivors retains the
established default exit policy; the warning and `state: incomplete` make
that condition explicit. Automation that requires complete cleanup must check
this field as well as the exit code. An attached program left running after
successful detach has `cleanup.state: complete`.

After startup succeeds, exit precedence is: a qualifying signal (`1`),
interruption (`130`), monitor/cleanup exception or JSON export failure (`2`),
budget breach (`5`), opted-in target failure (`4`), then opted-in inconclusive
measurement or incomplete coverage (`3`), otherwise `0`. `headless.exit_code` records the decision at
report construction. If JSON writing fails, no successfully written report
can promise to record that later failure; the terminal exit remains decisive.

`--require-full-coverage` (or `require_full_coverage = true`) is an opt-in
headless policy. A session needs at least one live collected sample; every
such sample must record full private-byte, handle, and thread coverage, and
no sample may have failed. Missing coverage records are unknown and do not
pass this policy. Paused samples are excluded. Warm-up samples count toward
coverage, while `--fail-on-inconclusive` separately requires a judged leak
window. Coverage is recorded as `coverage.full`, `measured_samples`, and
`fully_measured_samples`, also mirrored in `session_facts.coverage`; the
measurement outcome includes `full_coverage`.

The `experiment` section records the optional labels (`workload_id`,
`build`, `operations`, `note`), the mode, `arguments` (`count` and a
16-character SHA-256 `fingerprint` only), duration, sampling settings,
budgets, and the environment: OS name, release and version, architecture,
CPU count, total memory, Python version, and DrainTracer version. Host and
user names and environment variables are not recorded, and secret-looking
label values (`password=`, `token:`, `api_key=`, Basic/Bearer credentials, URL
credentials) become `<redacted>`. The fingerprint lets comparisons notice
different arguments; a short, guessable argument can still be confirmed by
hashing guesses, so it is not encryption. Headless reports add the checked
`budgets` under `headless`.

The target's console output is not included. Target paths, process names,
PIDs, timestamps, settings paths, and event messages may reveal information
about the local environment. Inspect a report before posting it publicly.

## Compare two runs

Run the same representative workload before and after a change, export both,
then pass the earlier report first:

```powershell
python src/main.py diff .\before.json .\after.json
python src/main.py diff .\before.json .\after.json --json
python src/main.py diff .\before.json .\after.json --fail-on-regression
```

Replace those paths with the actual exported JSON files. HTML reports are
for reading; `diff` accepts JSON reports. The comparison shows the strongest
signal, drift, and net growth for memory, handles, and threads, plus handle
category changes when available.

Signal changes are `clear`, `resolved`, `new`, `eased`, `worsened`, or
`persists`. Drift changes use a noise band so tiny fluctuations do not
automatically count as regressions. The combined verdict is `IMPROVED`,
`REGRESSED`, `MIXED`, or `UNCHANGED`; a mixed result has both improvements
and regressions across resources or measures.

By default a valid comparison exits `0` regardless of verdict.
`--fail-on-regression` returns `1` for `REGRESSED` or `MIXED`. Unreadable or
invalid reports, or a comparison failure, return `2`. With `--strict`, runs
that are not equivalent return `3` (this wins over the other codes), and a
budget flag (`--budget-private SIZE`, `--budget-handles N`,
`--budget-threads N`) exceeded by the later run's whole-run peak returns `4`.

Each reason the runs may not be equivalent is a caveat. Blocking caveats are
different targets, workload IDs, launch arguments, detector settings, or
operation counts (more than 10% apart, or recorded by only one run), and
runs more than 2x apart in length; these make a strict comparison
`NOT COMPARABLE`. A run that never filled a detection window, or partial
coverage, makes it `INSUFFICIENT COVERAGE`. Different machines and thinned
reports without exact facts only qualify the result. Without `--strict` any
caveat qualifies the verdict: the badge reads `IMPROVED (QUALIFIED)`, the
headline ends with the caveat count, and the output says what `--strict`
would report. A shorter or less demanding test can appear improved without
demonstrating a fix.

The comparison also shows each resource's whole-run peak (warm-up included,
exact when the report has session facts), so stable but excessive usage is
visible without a leak signal, and growth per operation when both runs
recorded `--operations`. The JSON adds `verdict_label`, `measured_verdict`
(the direction measured even when strict mode withholds it), `qualified`,
`strict`, `comparability`, `caveats` (`kind`, `message`, `blocks`),
`budgets`, `over_budget`, per-resource `metric`, `run_peak_before/after` and
`growth_per_operation_before/after`, and each side's `experiment` labels.
`verdict` stays the measured verdict unless strict mode withholds it. Keep the workload, machine conditions,
settings, warm-up, and duration as consistent as practical. Compare reports
from before v1.3 or v1.4 with care: they may lack settings or detection
coverage fields.
