# Privacy and safe sharing

This guide describes the current local implementation. The
[privacy policy](../PRIVACY.md) covers information practices and requests;
the [terms](../TERMS.md) explain licensing and operational responsibilities.

## What leaves the machine

The monitor does not upload reports, crash dumps, or usage telemetry.
Interactive startup with terminal output does automatically request release
metadata from `https://pypi.org/pypi/draintracer/json`, with a
`DrainTracer/<version>` user-agent. PyPI sees the request's source IP,
time, and headers. Target data is not included. The startup wait is bounded,
but a background request can outlast that wait.

Headless monitoring, `--help`, `--version`, report comparisons, and redirected
startup output skip the check. There is no dedicated interactive opt-out
flag. For a no-network workflow, install dependencies in advance and use a
mode that skips the check, or enforce your own network restrictions. Offline
interactive monitoring continues if the request cannot complete. A target
application's own network traffic is independent of DrainTracer.

`doctor` and `init` also skip the update check. Doctor performs local,
read-only target/PID checks and a temporary output-access probe; it does
not execute the target or send diagnostics. Its output contains local
paths, configuration origins, profile names, and numeric settings, so review
it before sharing. It excludes experiment free text, target arguments, and
environment values. `init` previews locally and writes a new configuration
only with `--write`; an existing file is never overwritten.

Pip installation, manual upgrades, Git fetch/pull, an accepted checkout
repair, and opening external links can also contact providers. Writing to
a shared or synchronized directory, enabling CI output/artifact publication,
or posting evidence sends information through those systems under your
control. A local path is not necessarily private storage.

## What a report can reveal

### Previewable privacy exports

Reports remain detailed by default. To inspect an explicit transformation
without writing files:

```powershell
draintracer-report redact .\run.json --profile share
draintracer-report redact .\run.json --profile minimal
```

`share` removes paths, free text, argument fingerprints, configuration
origins, host identifiers, unknown fields, and captured logs. It replaces
process IDs/names/creation identities with local pseudonyms and shifts all
recorded wall-clock dates to a fixed origin while preserving intervals.
Numeric resource evidence, coverage, and signal windows remain available.
`minimal` also removes event/marker detail and per-process resource
attribution. Per-process counters removed intentionally are marked unknown;
the aggregate measurements retain their original coverage semantics.

Add `--output-dir .\shared` to explicitly write a JSON/HTML pair. Both
formats are rendered from one transformed dictionary and identify the
profile; no original JSON or embedded HTML data is copied alongside them.
Each export receives a fresh report ID. Evidence is copied only from typed,
documented paths: an opaque metadata field named `value` or `operations`
cannot inherit metric permissions, and free-text fields are removed even
when malformed input supplies numeric or structured values. Marker indices
are renumbered. Unknown future fields are omitted instead of guessed safe.
Cleanup/startup/coverage outcomes retain their structured limitations while
diagnostic text and checkpoint identities are removed. Recovery retains only
its three Boolean qualification flags. Privacy-transformed reports always carry a
comparison caveat: pseudonyms cannot establish original target/workload
equivalence, so strict comparison refuses them.

Preview and export run locally without an account, upload, or telemetry.
The command does not transmit evidence. Saving into a synchronized/shared
folder can still send files through that service under your control.
Review both outputs before sharing: retained resource values and timing
patterns can identify a workload, so a transformed report is not a promise
of anonymity. Keep the original private report separately when needed.

### Detailed reports

Review all of these, even if you used a harmless workload label:

- `target_path`, `root_pid`, process names, parent IDs, creation times, and
  lifecycle records can identify a program or work session.
- `settings.config_path` and `settings.profile` can identify a user, customer,
  repository, project, or directory layout.
- `experiment` contains labels, notes, operation count, argument count and
  fingerprint, sampling details, budgets, and OS/Python/hardware information.
- `events`, `event_log`, `outcome`, snapshots, alerts, contributors, process
  summaries, and errors can repeat identities or expose free-text context.
- Timestamps include local UTC offsets. Durations, resource curves, process
  counts, and markers can reveal workload characteristics or activity times.
- The report's directory includes a target filename and date. File names,
  screenshots, terminal scrollback, and surrounding editor tabs may disclose
  details independently of the JSON fields.

HTML has its own embedded report data as well as visible text. Editing a
JSON file does not update an HTML file already exported beside it. Hiding
a value visually in a screenshot or browser is not deletion from the file.

Reports store counters rather than process memory contents. The process
picker enumerates visible processes locally. Handle classification reads
system-wide handle and TCP/UDP tables to produce monitored-process category
counts; those connection endpoints are not report fields. These operations
still need to be acceptable under your organization's monitoring rules.

## Redaction is limited

The experiment record omits raw target arguments, environment-variable
values, hostname, and username fields. It keeps the argument count and a
truncated SHA-256 fingerprint for launch comparisons. A predictable argument
can be checked by guessing and hashing it; the fingerprint is not a secure
way to conceal a secret.

Monitor diagnostics include host CPU utilization, available/total physical
memory, and the host reading's timestamp, age and collection cost. The detector
collects these locally at most once every five seconds; they are saved with
reports and are not attributed to the target. No new network request is involved.

Experiment labels pass through pattern-based removal of secret-looking
assignments, flags, URL credentials, and Basic/Bearer credentials. Arbitrary
names, paths, tokens in unexpected formats, configuration profile names,
events, and logs are not comprehensively sanitized. Values omitted as
dedicated fields can still appear inside paths or text.

There is no whole-report privacy profile, automatic identifier replacement,
or share preview in the current software. That work remains tracked in
[issue #41](https://github.com/JunaidAhamed-7777/DrainTracer/issues/41).

## Before sharing evidence

Privacy copies preserve typed clock-adjustment and sampling-gap counts and
relative timing-reset diagnostics. These qualify measurement continuity;
removing paths and identities must not remove a known evidence interruption.

1. Prefer a minimal reproduction using a disposable, synthetic workload.
   Avoid real customer paths, credentials, or production process names at
   collection time. Obtain authorization to share the resulting evidence.
2. Export to a folder with appropriate permissions. Keep the original private
   and work on a separate copy for sharing.
3. Inspect the entire JSON and HTML source, including embedded data, for
   paths, names, labels, IDs, fingerprints, errors, and other identifying
   values. Review any logs, screenshots, and filenames separately. Text search
   helps but cannot prove that an artifact contains no sensitive information.
4. For a support request, prefer the smallest sanitized text excerpt that
   preserves the issue: version, launch/attach mode, settings relevant to the
   result, metric units, interval, and coverage. Reports are optional. If you
   cannot confidently sanitize both formats, do not attach the full pair.
5. Mark changed excerpts as sanitized and explain omissions that affect
   interpretation. Keep comparisons on private originals: editing paths,
   argument fingerprints, or provenance can change comparability, and edited
   reports should not be presented as unmodified evidence.
6. Review the destination's visibility, recipients, retention, and backup
   behavior. Check CI logs, annotations, job summaries, and uploaded artifacts
   as separate publication surfaces. A public GitHub issue is not a private
   support inbox.

For suspected vulnerabilities or accidental sensitive exposure, use
[SECURITY.md](../SECURITY.md) or the [private contact route](../LEGAL.md#contact).
If a secret was posted, revoke or rotate it through its issuing system;
removing the visible post may leave copies in history or notifications.

## Storage, logging, and deletion

Default reports go below `leak_reports/reports`, relative to the current
working directory, unless `--report-dir` or configuration overrides it.
Headless runs attempt an export automatically at completion. Exports use
fresh report IDs, so repeated exports accumulate files. Long-session history
thinning limits in-memory detail; it does not expire saved reports.

Opt-in `--checkpoint PATH` saves report data, a session UUID, monitor/target
process identities, timestamps, and a checksum to a bounded local file. The
same session replaces its previous checkpoint; new sessions refuse existing
files. The checksum detects corruption and does not authenticate evidence.
Recovery writes new partial JSON/HTML reports. Checkpoints, recovered reports,
and the checkpoint's one-byte `.lock` sidecar are not automatically expired.
They are not encrypted or anonymous. Review all of them before sharing and
remove them under your own retention policy after the writer has stopped.

`DRAINTRACER_LOG` enables diagnostic file logging. Existing files are appended
to without built-in rotation. Unset it when finished:

```powershell
Remove-Item Env:DRAINTRACER_LOG -ErrorAction SilentlyContinue
```

That command stops opting future runs into file logging; it does not delete
the existing log or stop logging already active in a running process. An
embedding application's logging configuration can capture output separately.

Choose your own retention period. After the relevant run has stopped, remove
only the reports and logs you no longer need, including their HTML companions
and shared copies. Check CI artifacts, synced storage, terminal transcripts,
and backups separately. Uninstalling the package leaves user-created data
and project settings in place. The maintainer cannot remove local files for
you, and normal deletion does not promise secure erasure.

Use filesystem access controls and encryption appropriate to the data. The
application has no report password protection, encryption at rest, automatic
expiry, or remote wipe. Git ignores generated reports and logs, but an ignore
rule does not protect files from other users, backups, or a forced commit.

## Implementation references

For reviewers maintaining this guide:

- [updates.py](../src/updates.py) and [main.py](../src/main.py): update request,
  startup gating, checkout repair, and diagnostic logging.
- [running_processes.py](../src/running_processes.py),
  [process_runner.py](../src/process_runner.py),
  [leak_detector.py](../src/leak_detector.py), and
  [handle_types.py](../src/handle_types.py): process discovery and measurements.
- [experiment.py](../src/experiment.py): label cleaning, argument fingerprints,
  and environment fields.
- [session.py](../src/session.py), [report.py](../src/report.py), and
  [html_report.py](../src/html_report.py): report content, paths, and HTML data.
- [headless.py](../src/headless.py): automatic export and chosen CI outputs.

Changes to these data flows should trigger review of this guide and
[PRIVACY.md](../PRIVACY.md). Proposed hosted sharing and user research need
additional implementation and notices; these documents do not implement them.

## Optional local agent support

The opt-in agent stores a private credential, scheduling journal, checkpoints
and reports beneath a verified local user-controlled path. Its authenticated
pipe is local and never uploads evidence. `diagnose` is private operational
output and may contain local session/process identities, paths and error text;
do not paste it unreviewed. `support` previews an explicitly typed redacted
document, excludes credentials and raw operational metadata, and can include
up to two explicitly chosen privacy-transformed reports. JSON and offline HTML
share the same transformed report object. Archive creation requires `--out`;
there is no automatic publication. Numerical evidence can still identify a
workload. Removal previews/applies only known control files after shutdown;
reports, checkpoints and unrelated application files are preserved. See
[agent operations](agent-operations.md) before sharing or removing data.
