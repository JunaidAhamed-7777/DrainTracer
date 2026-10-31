# Optional local Windows agent contract

The agent is an explicitly started, foreground, per-user Windows process. It
does not install a service, register startup tasks, listen on a network port,
or grant extra operating-system privileges. Run it on a controlled development
machine under the same account that owns the monitored workloads. Unattended
service-account deployment needs a separate security and operations review.

## Trust and authentication

The local named-pipe endpoint includes a hash of the current token's user SID
and a validated profile name. Its protected DACL grants access only to that
user and SYSTEM, rejects remote pipe clients, and refuses a competing first
pipe instance. The agent state directory and credential have the same private
permissions. Redirected, network, inherited/public, or differently owned state
paths are rejected rather than silently repaired. Existing unrelated data is
preserved. A same-user process or administrator can access the credential or
change the agent; neither is an isolation boundary.

Each connection performs mutual HMAC-SHA256 authentication with independent,
fresh 256-bit challenges and role-separated proofs. The local credential is
never sent over the pipe, placed in command-line arguments, included in
diagnostics, or exported in reports. The client requests identification-only
security quality of service so a pipe server cannot impersonate its client.
Authentication errors reveal no session data. Requests and replies are bounded
JSON, never pickle or executable data; incomplete peers have I/O deadlines.

## Operations and capabilities

Every request has a protocol version, UUID request identity, operation name,
and operation-specific parameters. Every reply repeats that identity and
returns either a result or an explicit error code. A fresh UUID identifies
each session. IDs identify records; they are not credentials or authority.
The supported operations are:

- `start`: observe an existing target identified by PID **and creation time**,
  or launch a target when the server explicitly permits launches.
- `status`: list permitted sessions or inspect one session's current state,
  measurement progress, and target ownership.
- `events`: retrieve a bounded event page with a cursor and explicit eviction
  counts; absence of retained events never proves no events occurred.
- `export`: create JSON/HTML evidence in the agent-owned report directory;
  clients cannot select an arbitrary output path.
- `cancel`: finish one session and perform its ownership-specific cleanup.

Attachment defaults to observation. Launch, pause/resume, and explicit kill
are separate server-authorized capabilities; accepting a client connection
does not enable them. Launch authorization also restricts executable/script
paths to declared canonical local roots. It is permission to run trusted
code, not a sandbox: allowed workloads can themselves execute other code.
Attached targets are detached and any agent-owned pause is resumed during
ordinary cancellation/shutdown. A launched tree belongs to its session and
is stopped on cleanup. Kill is never inferred from attach permission.
The agent refuses attached-target pause/resume even when launch pause is
authorized: a crash between an OS suspension and a durable record cannot
prove ownership safely enough to resume that process after restart.

### Launch ownership on Windows

Agent launches currently accept trusted `.exe` and `.py` files beneath the
configured launch roots; use an explicit trusted wrapper for batch commands.
The agent creates the process suspended, assigns it to an unnamed,
noninheritable Windows job with kill-on-close enabled and breakaway disabled,
then resumes its primary thread exactly once. Failed assignment stops the
still-suspended process before user code can run. Only an explicit NUL handle
is inherited; stdin, stdout, and stderr cannot consume the agent console.
Ordinary cleanup closes the job, and abrupt agent exit closes it in the OS.

Normal `CreateProcess` descendants inherit the job. This is an ownership
mechanism for cooperating development workloads, not isolation for hostile
code: Windows documents exceptions such as children created through
`Win32_Process.Create`. Do not authorize untrusted launch roots. The regular
interactive CLI launch behavior remains separate from this opt-in adapter.

The implementation follows Microsoft's [job object ownership rules](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[suspended process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags),
and [primary-thread resume semantics](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread).
Native drills cover assignment failure before execution and abrupt agent
termination while an owned root and child run alongside an unrelated process.

## Bounds and failure semantics

Session count, concurrent workers, queued work, retained events, request size,
and request duration have explicit bounds. A worker owns each backend session;
the IPC handler consumes bounded copies and cannot run callbacks on the
sampling thread. One denied, failed, or slow workload does not end unrelated
sessions. Errors distinguish invalid input, unauthenticated/forbidden access,
unknown sessions, exhausted capacity, unavailable targets, storage failure,
and incomplete cleanup. An accepted request does not imply successful startup
or complete cleanup: status and evidence retain those outcomes separately.

Client disconnect does not silently cancel an accepted workload. Session
deadlines still apply. Recovery after an agent interruption must label old
sessions interrupted, preserve available evidence, and validate PID plus
creation time before any explicitly authorized process control. A checkpoint
or session ID never authorizes reattachment or another launch. Retry,
persistence, retention, and recovery policies are part of the scheduling
contract rather than promises of exactly-once operating-system side effects.

The native acceptance drills cover successful authenticated monitoring,
wrong-credential and other-principal rejection, malformed/truncated frames,
capability denials, PID reuse, ownership-specific cleanup, and unrelated
concurrent workloads. Crash/restart, disconnect, disk exhaustion, and removal
drills additionally qualify the scheduling and operations implementation.

## Review boundaries

The foreground per-user/SID ACL/mutual authentication/capability contract was
reviewed before implementation. Protocol evolution must preserve strict
input validation, bounded resource use, ownership checks, and explicit
failure states. Changes to authentication, endpoint exposure, service
accounts, privilege, or automatic persistence require another threat review.
Reports retain the normal [privacy and sharing requirements](privacy.md).
