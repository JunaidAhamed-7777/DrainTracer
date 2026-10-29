# Privacy policy

Effective and last updated: **28 October 2026**.

This policy describes the official DrainTracer project maintained by
[Junaid Ahamed](https://github.com/JunaidAhamed-7777) (the "maintainer", "we", or
"us"). It covers the local Windows CLI/TUI, its generated reports, and
information you choose to provide through project support and contribution
channels. Read it with the [terms](TERMS.md) and
[practical privacy guide](docs/privacy.md).

## 1. The essentials

- Monitoring runs locally. DrainTracer has no project-operated collection
  server, account system, advertising SDK, usage analytics, or automatic
  report or crash upload.
- Interactive startup automatically checks PyPI for a newer release. PyPI
  receives the connecting IP address and a request identifying the installed
  DrainTracer version. This is a network request even if you never export
  or share a report.
- Monitoring records can contain personal or confidential information,
  especially in paths, process names, labels, event messages, and logs.
  Existing redaction is limited; reports are not anonymous by default.
- Exported JSON and HTML, diagnostic logs, and CI output remain where you or
  your environment save them until removed under your own retention settings.
- The maintainer receives monitoring evidence only if you or someone with
  access chooses to share it. Public GitHub posts can be copied by others.

## 2. Who handles the information

For local monitoring, you or the organization operating DrainTracer decides
which processes to monitor, the purpose, storage location, access, sharing,
and deletion. Where data-protection law applies, that operator must determine
its role and responsibilities. Merely supplying the software does not give
the maintainer access to the operator's local process data or make the
maintainer its hosted data processor.

The maintainer handles information received through project communications
for the purposes below. GitHub, PyPI, your email provider, CI provider,
employer, and any storage or backup service separately handle information
under their own policies and applicable arrangements.

## 3. Information processed on your machine

### Process selection and monitoring

The running-process picker reads names, process IDs (PIDs), and creation
times for visible processes to let you select a target. Monitoring uses the
selected process and discovered descendants, including relevant parent and
lifecycle information. It can record:

- Target executable or script path, process names, PIDs, parent PIDs,
  creation times, launch/attach mode, starts, exits, restarts, and exit status.
- CPU usage; resident, private, and virtual memory measurements; handle and
  thread counts; I/O byte rates; and available handle-category counts.
- Sampling timestamps, duration, coverage gaps, errors, detection windows,
  resource-growth evidence, severity, peaks, and process contributions.
- DrainTracer's own resource use and collection timing, for monitor diagnostics.

Windows handle classification queries system-wide handle information and
TCP/UDP connection tables locally, then uses counts for monitored PIDs. It
does not send network packets to those connections or export their endpoint
addresses as part of handle classification. The broader queries are needed
to derive the selected processes' counts; they are not a remote collection
of every process's report.

Resource sampling reads operating-system counters. It does not capture
keystrokes, screen contents, packet payloads, document contents, or process
memory contents as a monitoring feature. It does not capture the launched
target's console output: stdin, stdout, and stderr are redirected to the
null device. A target may independently read, write, or transmit data.

### Settings and experiment records

DrainTracer reads the settings you supply or that it discovers locally.
Reports include the active profile name, configuration-file path, sampling
settings, thresholds, warm-up, timeout, and applicable budgets. Optional
workload ID, build label, operation count, and note identify your experiment.

Reports also include operating-system and Python versions, machine
architecture, CPU count, total physical memory, and DrainTracer's version.
There are no dedicated hostname or username fields in this environment
record, but usernames, customer names, and machine identifiers can still
appear in paths or free text.

For launched targets, the experiment record stores the argument count and
the first 16 hexadecimal characters of a SHA-256 fingerprint of the argument
list, rather than the raw arguments. Attached targets have no such argument
record. A fingerprint is not encryption or anonymization: someone can test
guesses against a short or predictable argument list.

Environment-variable values are not copied into the experiment record.
DrainTracer still reads operational environment settings, such as the log
destination, command interpreter, and CI summary destination, and launched
processes inherit their execution environment. Do not interpret the report
omission as isolation of a target from environment secrets.

### Reports, console output, and diagnostic logs

An export creates a JSON report and attempts an accompanying self-contained
HTML report. Each export has a random report ID used in its filenames; this
is not a persistent user or installation identifier. Reports include session
metadata, measurements, process details, settings, events, markers, and
outcomes. Filenames and containing folders can reveal the target name and
date even when a file's contents are edited.

Interactive export uses the dashboard controls. Headless monitoring attempts
an export at the end of the run, including handled interruptions. The default
root is `leak_reports/reports` relative to the working directory; settings or
`--report-dir` can select another location. Configuration-relative paths are
explained in the [configuration guide](docs/configuration.md).

Optional `--checkpoint PATH` writes a bounded local recovery file at the chosen
cadence. It contains report data plus a session UUID, monitor and target process
identities, cutoff/write timestamps, and a corruption checksum. The UUID links
checkpoints within that session; it is not an installation or user identifier.
The current session replaces its earlier checkpoint atomically, while a new
session refuses an existing file. Recovery creates additional explicitly partial
JSON/HTML reports without attaching to or controlling a process. Checkpoints and
their lock sidecars remain until you remove them; they have no automatic expiry,
encryption, or anonymization. Review them as carefully as ordinary reports.

Setting `DRAINTRACER_LOG` enables a local diagnostic file containing error
messages, timestamps, logger/thread names, and tracebacks. These can reveal
paths and other context. An embedding application's existing logging
configuration may also capture diagnostics. Console output can be retained
by terminal transcripts, shell history, screen recordings, or CI logs.

### Limits of redaction

Experiment labels are shortened and certain secret-looking assignments,
secret flags, URL credentials, and Basic/Bearer credentials are replaced.
This is pattern matching on those labels, not comprehensive sanitization of
paths, events, profiles, logs, screenshots, or the whole report. There is no
implemented previewable privacy-profile export. Review both JSON and HTML,
including embedded data, before sharing. See [the sharing procedure](docs/privacy.md#before-sharing-evidence).

## 4. Network requests and third-party services

### Automatic PyPI update check

Interactive startup with terminal output makes an HTTPS request to
`https://pypi.org/pypi/draintracer/json`. The request includes
`User-Agent: DrainTracer/<runtime-version>` and asks for JSON release
metadata. PyPI and the network infrastructure carrying the request can
observe connection metadata, such as the source IP and request time; PyPI
can also see the request headers. DrainTracer does not include process
metrics, reports, target paths, target arguments, or labels in that request.

The startup wait is bounded to about one second. A background request may
finish later; the wait limit is not a guarantee that all network activity
ends at that moment. The check displays availability information and does
not install an update or upload evidence.

Headless runs, help, version-only commands, report comparisons, and startup
with redirected output do not perform this update check. There is currently
no dedicated interactive update-check opt-out flag. Monitoring can work
offline after its prerequisites are installed; an unavailable check does
not prevent monitoring. Use a mode that skips the check or your network
controls when a no-network workflow is required.

PyPI is operated by the Python Software Foundation. Its
[PyPI privacy notice](https://policies.python.org/pypi.org/Privacy-Notice/)
and the linked PSF notice explain the provider's own handling and retention.
We do not control those records.

### Installation, repair, links, and CI

Installing or updating with pip contacts the configured package index and
download hosts; using a GitHub archive or Git contacts GitHub or the
configured remote. A checkout's missing-file repair contacts `origin main`
only after the interactive repair choice. Package-manager, Git, proxy, DNS,
and operating-system behavior is separate from process sampling.

Opening a project link sends your browser to its destination. GitHub-hosted
documentation, badges, and linked resources are governed by their providers.
The local CLI does not set browser cookies, use advertising pixels, or
implement cross-site tracking. The generated HTML report embeds its styles,
scripts, and report data and requires no CDN, analytics, or remote report
server to display. Your browser and extensions have their own behavior.

When you choose GitHub Actions annotations or a job summary, DrainTracer
writes output or a runner-local summary file that the runner can publish.
A workflow may also upload report artifacts. This can expose evidence
according to the repository's visibility, workflow permissions, and artifact
retention. It is not a private upload to the DrainTracer maintainer.

See [GitHub's privacy statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement)
for GitHub's account, repository, and service data practices.

## 5. Information you send to the project

If you open an issue, submit a pull request, send a private message through
a published contact route, or report a vulnerability or conduct concern,
the maintainer may receive your name or username, contact details, message,
attachments, technical environment, and correspondence history. Git commits
can also disclose their recorded author name and email address.

We use that information to respond, reproduce and resolve problems, review
contributions, maintain project history, address security or conduct
concerns, and meet applicable legal obligations. Do not submit personal
data about other people without an appropriate basis or permission. Reports
and screenshots are optional; a sanitized minimal reproduction is preferred.

Public issues, comments, pull requests, commits, and attachments are visible
to others. Private reports may be shared with people who need them to
investigate or address the concern, with relevant service providers, or
where legally required. We aim to limit such sharing to what is necessary;
do not assume absolute confidentiality or send secrets as proof of a bug.

The project does not sell personal information, use submitted data for
targeted advertising, or share it for cross-context behavioral advertising.
There is no automatic enrollment in marketing or research by running the
tool or opening an issue. Any future optional research or data-upload
program needs its own notice and choice before collection.

## 6. Purposes and applicable legal bases

Local measurements serve the monitoring and comparison purposes chosen by
the operator. That operator must assess its authority to monitor, provide
any required workplace or other notices, and identify its own legal basis.
This policy does not authorize monitoring someone else's device or data.

Where a law such as the EU or UK GDPR applies to the maintainer's handling
of submitted personal data, the relevant bases are legitimate interests in
maintaining and securing the project, answering requested support, reviewing
contributions, and managing abuse; compliance with a legal obligation when
one applies; and consent where a particular optional activity requires it.
Those interests must be balanced against affected individuals' rights.
The maintainer does not treat installation as blanket consent to unrelated
processing. Local use does not create a hosted service contract or give us
consent to receive monitoring reports.

## 7. Retention, deletion, and security

Local session records are held in memory while the process runs, with bounded
histories and thinning for long sessions. Those bounds manage memory use;
they do not anonymize data or implement a disk-retention policy. Operating
systems may separately retain swap, crash dumps, backups, or transcripts.

Exported reports and diagnostic logs have no built-in expiry, automatic
deletion, or application-level encryption. Diagnostic file logging appends
without built-in rotation. You control deletion, filesystem permissions,
encryption, backups, synchronized folders, and CI retention. Uninstalling
DrainTracer does not remove your reports, logs, or project settings, and
ordinary file deletion is not guaranteed secure erasure.

Public contributions and discussions are generally retained as project
history while the repository and hosting service retain them. Private
support, security, conduct, and legal correspondence is kept for as long as
needed to handle the matter, prevent recurring abuse, or meet applicable
legal obligations and resolve disputes. This is purpose-based retention,
not a promise of automatic deletion on a fixed date. You may request review
or deletion of information held by the maintainer.

Repository deletion or editing cannot recall copies in forks, clones,
notifications, caches, third-party archives, or someone else's backups.
The maintainer cannot delete records controlled solely by other providers.
Access restrictions and careful sharing reduce risk, but no software,
storage system, or communication channel guarantees complete security.

## 8. Your choices, requests, and rights

You can run the local software without an account, choose the target and
settings, omit optional labels, leave diagnostic file logging disabled,
choose private output folders, and decide whether to share evidence.
Headless operation still writes reports; opting out of sharing is not the
same as disabling local storage. Stop using the tool or remove local files
when you no longer need them, taking account of target cleanup behavior.

Depending on applicable law, you may have rights to information, access,
correction, deletion, restriction, portability, or objection, and to withdraw
consent where processing relies on it. Withdrawal does not affect lawful
processing before withdrawal. You may also complain to your applicable
data-protection authority and exercise rights without unlawful discrimination.
The availability and limits of each right depend on the circumstances;
these statements do not promise a right that the law does not provide.

For information held by the maintainer, use the private contact route in
section 10. Identify the relevant post or correspondence and your request;
do not include identity documents or extra sensitive information unless
needed through an agreed private channel. We may ask for proportionate
verification, respond within applicable legal deadlines, and explain any
lawful reason a request cannot be fully fulfilled. For local files, act on
your device or contact your organization. For platform-held data, contact
the platform directly.

The tool's resource-growth heuristics are technical diagnostic results;
they are not used by the project to make decisions about people's legal
rights or eligibility for services.

## 9. International access and children

Local monitoring does not automatically transfer reports to another country.
If you share information through GitHub or another provider, it may be
processed in the countries where the provider and recipients operate. Public
material is accessible worldwide. See each provider's policy for its
transfer arrangements; this policy does not promise a particular data region
or create a data-processing agreement.

DrainTracer is a developer tool and is not directed at children. It has no
account or age-verification system and does not ask for age information.
Do not submit children's personal information to project channels. If you
believe such information was submitted inappropriately, use the private
contact route. Third-party platforms apply their own age and account rules.

## 10. Contact and policy changes

The responsible project contact is
[Junaid Ahamed, repository maintainer](https://github.com/JunaidAhamed-7777).
Use a private contact method published on that profile. If none is available,
open an [issue](https://github.com/JunaidAhamed-7777/DrainTracer/issues/new)
titled **Private contact requested** asking only for a private privacy/legal
channel. That request itself is public; leave out personal details and
evidence. Vulnerabilities follow [SECURITY.md](SECURITY.md).

We will update this policy when relevant practices change, update its date,
and keep changes in repository history. Material new collection or a new
service requires appropriate notice and, where required, consent before it
starts. A policy update does not retroactively authorize a new use of data
that requires a separate legal basis or permission.
