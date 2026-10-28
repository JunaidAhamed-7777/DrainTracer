# Terms of use

Effective and last updated: **28 October 2026**.

These terms explain use of DrainTracer's official software, documentation,
and project spaces, maintained by
[Junaid Ahamed](https://github.com/JunaidAhamed-7777) (the "maintainer"). They
also serve as the terms of service for those project resources. DrainTracer
is currently a local open-source diagnostic tool, with no hosted monitoring,
report-storage, paid subscription, or account service operated by the project.

## 1. License and priority

The [MIT license](LICENSE) is the operative software license. It permits use,
copying, modification, distribution, sublicensing, and sale subject to its
notice requirements, and contains the software's warranty and liability
disclaimers. It applies to the project's software and accompanying
documentation except where a separate notice identifies another license.
[Third-party components](THIRD_PARTY_NOTICES.md) retain their own terms.

These terms explain operational responsibilities and govern participation
in project-controlled spaces to the extent applicable. They do not add
field-of-use, commercial-use, redistribution, or acceptance restrictions to
the MIT license. No click-through agreement is required to exercise MIT
permissions. If this document conflicts with the MIT license on software
rights or conditions, the MIT license controls. Mandatory applicable law
also prevails.

The [privacy policy](PRIVACY.md) explains information practices; it is not a
request for blanket consent or a separate data-processing agreement. The
[code of conduct](CODE_OF_CONDUCT.md) applies in project community spaces.

## 2. What the tool does and does not provide

DrainTracer launches an executable or script, or attaches to a selected PID,
tracks its process tree, samples operating-system resource counters, and
reports sustained growth using heuristics. It can generate JSON and offline
HTML reports and compare saved runs. Supported monitoring is on Windows as
described in the [README](README.md).

A signal is evidence for investigation, not proof of a leak or identification
of a source-code defect. A signal-free run is not a certification that an
application is correct, secure, leak-free, or suitable for production.
Short runs, warm-up, permissions, process races, sampling gaps, observer
overhead, and workload differences can affect results. Review coverage,
measurement validity, duration, and comparability before relying on a result.

The tool is not a sandbox, antivirus product, forensic evidence-preservation
system, real-time safety system, or compliance certification service. It
does not promise to detect every resource problem or to work in every
environment. Roadmap proposals are not delivery commitments.

## 3. Authorized and responsible operation

You are responsible for your authority to execute, inspect, suspend, resume,
or terminate a target and its descendants. Obtain any permission required
from the system owner, employer, customer, or other affected party. Comply
with applicable law, workplace policies, confidentiality duties, and licenses
governing the target and its data.

Use only targets you trust and understand. A launched target executes with
the permissions and environment available to the DrainTracer process;
selecting it does not make it safe. Elevation may expand access and impact.
Assess the effects on shared services and other users, use a suitable test
environment, and keep backups appropriate to your workload.

These responsibilities describe independent legal and operational duties;
they are not additional restrictions on the MIT software license. Nothing
here grants permission to access someone else's systems, secrets, or data.

## 4. Process control and loss risks

Ordinary cleanup, quitting, or a timeout stops a target launched by
DrainTracer and attempts to stop its tracked tree. Termination may interrupt
writes, discard unsaved work, or leave application state requiring recovery.
An attached target ordinarily keeps running when you quit or time out.

Dashboard pause/resume affects the target as well as sampling. The explicit
kill control affects the monitored tree, including attached targets. A
confirmation or protected-ancestor guard reduces some risks but does not
make every target safe to control. Access denial, process exits, identity
changes, and operating-system failures can prevent complete cleanup or
resumption. Inspect reported failures and the remaining processes.

Launched targets have console input and output redirected to the null
device. They cannot receive interactive input through DrainTracer and their
console logs are not preserved by it. Configure target-side logging when
needed, and use [the usage guide](docs/usage.md) to understand each control
before use. You remain responsible for recovering your application and data.

## 5. Your evidence and privacy responsibilities

The maintainer does not claim ownership of your programs, measurements,
reports, or other data merely because you use DrainTracer. Existing rights
in the report's software, template, and third-party components remain with
their owners. Exporting a report does not transfer it to the maintainer.

You choose what to monitor and where outputs are stored or shared. Reports,
logs, console output, and screenshots can contain paths, process identities,
timestamps, labels, system characteristics, and sensitive text. The limited
built-in redaction is not a guarantee that an artifact is safe to publish.
Review every format and follow [the privacy guide](docs/privacy.md).

Protect output with appropriate access controls and retention. A CI runner,
cloud-synchronized folder, email attachment, or public issue can make a
local artifact available to others. You are responsible for the authority
and notices needed when processing or sharing other people's information.
There is no project-provided encrypted report vault, deletion service, or
backup recovery service.

## 6. Dependencies, network access, and external services

Interactive startup automatically checks PyPI for release metadata. This
reveals connection information and the DrainTracer version to the provider,
but does not send process reports. The check does not install an update.
Other modes, network-control options, installation requests, and checkout
repair behavior are described in [PRIVACY.md](PRIVACY.md#4-network-requests-and-third-party-services).

GitHub, PyPI, package indexes, browsers, CI systems, target applications, and
dependency suppliers operate under their own terms and policies. Linking
to or using them is not a warranty or endorsement of their availability,
security, accuracy, or suitability. DrainTracer's maintainer cannot control
their service changes or retrieve their deleted data.

Check the source and applicable licenses when obtaining a copy, fork, plugin,
dependency, or report. Treat third-party HTML and other attachments as
untrusted content. No hosted sharing service or remote agent is authorized
or offered merely because a roadmap describes one.

## 7. Contributions and public participation

Follow [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md) when participating. Submit only material
you have the right to share. Do not post credentials, private customer data,
or other confidential material. Use [SECURITY.md](SECURITY.md) for sensitive
vulnerability details.

By intentionally submitting code, documentation, or other material for
inclusion in the project, you offer that contribution under the project's
MIT license unless a different compatible arrangement is expressly agreed
before inclusion. You retain your copyright; no copyright assignment is
required. Identify third-party material and preserve its required notices.
Ordinary support correspondence does not assign ownership of your data or
relicense a third party's confidential material.

Submissions may be discussed, edited, declined, or incorporated at the
maintainer's discretion. Acceptance, attribution format, review time, and
future maintenance are not guaranteed beyond applicable license obligations.
Public submissions may persist in Git history, forks, or platform archives.

Maintainers may moderate project spaces under the code of conduct, including
removing inappropriate content or restricting participation. Losing access
to a project discussion does not revoke the MIT permissions for software
copies you lawfully received. Platform moderation and account rules also apply.

## 8. Support, updates, and continuity

Community support is offered as availability permits. There is no guaranteed
response time, fix, release schedule, service level, compatibility period,
backport, or uninterrupted repository availability. See [SUPPORT.md](SUPPORT.md)
and [SECURITY.md](SECURITY.md) for current routes and maintenance scope.

You choose whether and when to install an update and should validate it
against your environment. Review [release notes](RELEASES.md). Semantic
Versioning describes release intent; it is not a guarantee that every
dependency, report consumer, or workload will remain compatible.

The maintainer may change or discontinue project resources. Such changes do
not withdraw the license granted for an existing copy. There are currently
no project subscription charges, renewal terms, or refund arrangements;
third parties may charge separately for their own services or distributions.

## 9. Warranties and liability

The software is provided **as is**, under the warranty exclusions and
limitation of liability in the [MIT license](LICENSE). Those provisions cover
the authors and copyright holders and remain the authoritative wording.
Documentation, examples, reports, and support do not create an additional
warranty, professional advisory relationship, or promise of fitness for a
particular purpose.

Operational consequences can include downtime, lost work, incomplete
cleanup, incorrect conclusions, disclosure through shared evidence, or
resource consumption. Assess these risks before relying on the tool.
To the extent permitted by applicable law, the license's disclaimer and
limitation apply to claims arising from the software or its use.

Nothing in these terms excludes a mandatory consumer right or liability
that applicable law does not allow to be excluded or limited. If a provision
cannot legally apply to you, the remaining valid provisions continue to
apply to the extent permitted. This document adds no indemnity obligation,
mandatory arbitration clause, or class-action waiver.

## 10. Names, attribution, and redistribution

Keep the copyright and permission notices required by the MIT license in
copies or substantial portions of the software. Preserve third-party
notices and satisfy the licenses of components you redistribute, including
dependencies packaged into a standalone executable or installer.

The MIT license does not itself grant trademark rights. Do not represent a
modified or independently hosted version as an official release or imply
maintainer sponsorship without permission. This does not prohibit truthful
attribution, references allowed by law, or the licensed reuse of project
materials. See [the brand guide](docs/branding.md) for asset conventions.

## 11. Questions, disputes, and changes

For licensing, legal, privacy, or rights concerns, use a private contact
method published on [the maintainer's profile](https://github.com/JunaidAhamed-7777).
If none is available, open an
[issue](https://github.com/JunaidAhamed-7777/DrainTracer/issues/new) titled
**Private contact requested**, asking only for a private channel. Do not
publish the allegation, personal details, or confidential evidence.

Contact is encouraged to resolve a concern, but it does not prevent you from
exercising legal rights or meeting a legal deadline. These terms do not
choose an exclusive court or governing jurisdiction; applicable law and
ordinary jurisdiction rules determine those questions.

Changes will carry an updated date and remain visible in repository history.
They do not retroactively change the license on copies already received or
create acceptance of a new service agreement simply because you continue
using an existing copy. Any future hosted or paid offering would need its
own applicable terms before you elect to use it.
