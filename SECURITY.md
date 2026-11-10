# Security policy

## Reporting a vulnerability

Please avoid publishing exploit details in a public issue or pull request
before a fix can be discussed.

1. Check the repository's
   [Security page](https://github.com/JunaidAhamed-7777/DrainTracer/security).
   If **Report a vulnerability** is available, use that private reporting
   route. Its availability depends on the repository's GitHub settings.
2. Otherwise, use a private contact method published on
   [the maintainer's profile](https://github.com/JunaidAhamed-7777).
3. If neither is available, open an
   [issue](https://github.com/JunaidAhamed-7777/DrainTracer/issues/new) titled
   "Private security contact requested". Include only a request for a private
   channel, with no reproduction, exploit, or sensitive information.

In the private report, include the affected version or commit, operating system and
Python versions, prerequisites, expected impact, and a minimal reproduction.
Use a harmless test target and remove secrets and identifying information.

Security fixes are developed against the current `main` branch. Separate
maintenance branches, backports, response times, and release dates are not
guaranteed. Check the latest source before reporting an already fixed issue.

## Security boundaries

DrainTracer launches executable code and can pause or terminate the monitored
process tree. A selected executable or script runs with the permissions of
the user running DrainTracer. DrainTracer is not a sandbox or a malware
analysis environment. Run only targets you trust; use elevation only when
your monitoring task requires it.

Launched targets are stopped on session cleanup. Attached targets normally
keep running when you quit, but explicit pause and kill controls affect them.
See the [usage documentation](README.md) before attaching to a shared service.

Reports and diagnostic logs can contain executable and configuration paths,
process names, PIDs, timing, event text, and resource measurements. Review
both JSON and HTML reports before sharing them. Treat reports from other
people as untrusted files and review their origin before opening HTML.

An access-denied metric or a heuristic false positive belongs in the normal
bug tracker unless it demonstrates a security impact.

## Optional local Windows agent

`draintracer-agent` is opt-in and runs in the foreground as the current user.
It installs no service and opens no network port. Its named pipe has a
protected DACL that admits only that user and SYSTEM and rejects remote
clients; its state directory and credential are private to the user. Each
connection performs mutual HMAC-SHA256 authentication, and the credential is
never sent over the pipe. Attaching only observes: launch, pause/resume, and
explicit kill require the server flags `--allow-launch` (limited to declared
`--launch-root` directories),
`--allow-pause`, and `--allow-kill`. A same-user process or an administrator
can still read the credential or change the agent, so this is not an
isolation boundary. Do not run the agent elevated; run it under the ordinary
account that owns the workloads. See [the agent contract](docs/local-agent.md).

## Optional sharing and upload queue

`draintracer-share serve` is an opt-in, foreground TLS listener on an
operator-controlled host. It binds to `127.0.0.1` by default; another address
requires `--host ADDRESS --allow-network`, and firewalling and supervision
remain the operator's job. Certificate, hostname, and protocol verification
are mandatory, with no cleartext fallback. Clients read bearer credentials
from private files, never command-line arguments; protect, rotate, and revoke
them like passwords. Project roles, revocation, retention, and deletion are
enforced by the server's live team database.

The upload queue is a private local directory that receives entries only
from explicit `enqueue-*` commands. It stores privacy-transformed payloads, never
credentials, and only `flush` sends entries. Monitoring and local export never
depend on it. See [team sharing](docs/team-sharing.md) and
[team access](docs/team-access.md).

## Reports, network access, and disclosure

The application has no automatic report or crash upload. Interactive startup
does make a PyPI release-metadata request; [PRIVACY.md](PRIVACY.md) describes
its fields and the modes that skip it. There is no dedicated opt-out flag for
that check; use a mode that skips it or your own network controls. Local
reports have no application-level encryption or expiry, and outside the
optional sharing server above there is no access-control service for them.
Filesystem and CI permissions remain the operator's responsibility. A
target's own network and file access are separate from DrainTracer's
behavior.

Limited label cleaning and argument fingerprints do not make a report
anonymous. The [safe sharing guide](docs/privacy.md) explains how identifiers
can recur in JSON, embedded HTML data, logs, and filenames. For an accidental
credential disclosure, revoke or rotate the credential with its issuer;
removing an attachment may not remove historical or copied versions.

Test only systems and workloads you are authorized to test. Keep reproductions
bounded, avoid real personal data, and do not disrupt other users or access
their information to demonstrate an issue. Coordinate publication of details
with the maintainer where possible so users can receive a fix or mitigation.
Reporting a vulnerability does not itself authorize further access to any
system or promise immunity from third parties' claims.

This project has no standing paid bug-bounty program or guaranteed reward.
Recognition and disclosure timing can be discussed privately. Necessary
details may be shared with people involved in remediation; confidentiality
cannot be guaranteed. [The privacy policy](PRIVACY.md#5-information-you-send-to-the-project)
also applies to information voluntarily sent in a report.
