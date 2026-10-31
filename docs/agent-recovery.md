# Agent scheduling and recovery policy

The per-user foreground agent has no launch retry loop, startup registration,
or unattended service account. Each accepted session has a finite deadline.
There is no pending launch queue: the active-session cap rejects excess work.
One backend failure or disconnected client does not cancel another session.

## Durable ownership intent

The agent owns an OS-released exclusive lock for its private state directory,
including across different endpoint profiles. Before starting a backend it
atomically saves the accepted session UUID, request UUID and digest, deadline,
launch/attach mode, and known process identity. It never persists executable
arguments or environment values in the scheduling journal. Reports and
checkpoints retain the usual private evidence contract separately.

Repeated start requests with a retained request UUID return the original
session, including after an agent restart. A different request under that UUID
is a conflict. Request retention is bounded; the client never automatically
retries a start or control command after an uncertain failure. After retained
history expires, a new explicit start requires a new request UUID. No record
claims exactly-once operating-system side effects across a crash.

The journal is schema- and version-checked, bounded, and atomically replaced
only after flushing its new contents. Invalid, future-version, or unrelated
state is preserved and blocks startup. A storage failure blocks new starts
before launch, exposes an unhealthy storage status, and preserves the previous
journal. Active session cleanup still runs. Evidence writes can fail separately;
a successful request or accepted start never guarantees a complete report.

## Restart and interrupted evidence

On restart, every saved accepted/starting/running/stopping session becomes
`interrupted`. The agent does not instantiate its former backend, retry a
launch, reattach, pause, resume, or kill a recorded PID. A read-only PID and
creation-time check reports `identity_matches`, `reused`, `exited`, `unknown`,
or `unavailable`. A matching identity is a diagnostic, never authorization for
control. A launched job is killed when the former agent's OS handle closes;
attached targets were observed without an owned pause and remain untouched.

Agent sessions use bounded periodic recovery checkpoints in their private
state directory. The latest intact prefix can be exported as a fresh
JSON/HTML pair after restart. It is explicitly partial: facts are exact only
through the cutoff, the missing tail is unknown, and final outcome and cleanup
are unconfirmed. No checkpoint is an instruction to repeat process actions.
A crash before the first valid checkpoint leaves an interrupted record with
`evidence_unavailable`, rather than invented evidence.

## Retention, storage, and shutdown

The journal retains at most the configured session-record count and a bounded
set of start-request tombstones. Completed records expire after a declared
retention period; report and checkpoint files are preserved until explicit
local removal. New admission and manual export use a declared storage
high-water limit and bounded inventory. This is an admission limit, not a
filesystem quota: final cleanup evidence from already accepted sessions may
cross it. Use an OS-enforced disk quota when a hard storage ceiling is needed.
A full disk produces explicit storage/export failures and preserves any
previous intact checkpoint or journal.

Ordinary shutdown stops admission, requests cancellation for all active
sessions, and waits against one shared finite deadline. Each session releases
its attachment or stops its owned launch; incomplete cleanup remains visible.
Client disconnect, endpoint profile changes, or upgrade do not silently adopt
old process ownership. Upgrade, rollback and removal procedures must preserve
this journal/evidence boundary and never delete unrelated application data.

## Recorded acceptance drills

Native Windows tests start separate agent processes, monitor an owned root
and child, kill the agent abruptly, and restart against the same state. They
verify interrupted/unknown-cleanup labels, partial-prefix export, durable
start replay without another launch, and OS job cleanup. A second drill drops
a client connection without acknowledging a reply, then kills/restarts the
agent while an attached target continues running with the same identity.
A failed target and an admission high-water condition leave another accepted
session sampling and allow its cancellation/cleanup. Atomic-write fault tests
inject full-disk-style fsync/replace errors and preserve the prior intact
journal; an actual system disk is never intentionally filled.

Defaults are seven days of completed/interrupted metadata and a 512 MiB
storage admission high-water mark. Retention is configurable from one second
through 90 days; storage admission is configurable from 1 MiB through 64 GiB.
The inventory reads at most 4,096 non-redirected entries with four directory
levels. No file is deleted automatically. A previously unobserved active
record is marked interrupted on its first restart even when older than the
retention period. Its fresh interrupted record then follows retention.
