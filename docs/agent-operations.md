# Local agent operations and support

The agent is installed with the Python package and started explicitly in the
foreground. There is no system service, startup entry or network listener to
remove. The same authenticated local client reuses session listing, progress,
cancellation and JSON/HTML evidence. Installation or a connected client never
grants launch, pause or kill capabilities by itself.

## Diagnostics and recovery

`draintracer-agent diagnose` reports responsive/readiness status, active and
retained counts, queue depth, observation age, stale sessions, storage admission
limits and bounded storage errors. It falls back to read-only offline journal
inspection when the agent cannot be reached; offline state never claims that
a saved active session is still running or complete. Diagnostic inspection
never launches, attaches, resumes or kills a process.

`draintracer-agent recover SESSION_UUID` previews the assigned intact checkpoint
as explicitly partial evidence without starting the agent. `--out-dir DIRECTORY`
writes a fresh JSON/HTML pair. Writer, target and checkpoint identities must
match; missing or corrupt evidence is an explicit error. Live evidence uses
the existing `export SESSION_UUID` command and opens in the accessible offline
HTML report, with its matching JSON beside it.

## Stop, upgrade, rollback and removal

`draintracer-agent stop` requests ordinary authenticated shutdown. Its reply
acknowledges the request, not completed cleanup. Stop admission, cancel all
active sessions, release attachments and stop owned launches; inspect the
stopped journal/diagnostics before proceeding. A client disconnect does not
revoke an accepted cancellation. Never substitute an unverified PID kill.

Install in a user-controlled virtual environment beneath a trusted private
ancestor. Pin the exact published version when repeatability matters. Before
upgrade, stop the agent, confirm no active writer, preserve the entire private
state directory and install the new wheel in a separate environment. Inspect
its version/help and diagnose the existing state before explicitly starting.
Rollback uses the previous environment and its compatible state backup;
future-version state is preserved and refused, never rewritten to force a
rollback. Agent credentials, checkpoints and journals remain private local
data throughout. Do not copy them into bug reports.

`draintracer-agent remove` previews removal of the known control files only.
`--apply` requires a stopped agent and no unresolved known launched process.
The validated journal is removed last so a partial file-removal failure can
be retried without guessing about ownership. Missing/corrupt authority blocks
removal when control files remain. It never sends process-control signals and never recursively deletes a state
directory. Reports, checkpoints and unrelated files are preserved. Remove
those later under an explicit local retention decision. Uninstall the package
from its virtual environment with that environment's `python -m pip uninstall
draintracer`, or remove only the disposable environment you created. Existing
application data and other environments are outside this operation.

## Previewable support bundle

`draintracer-agent support` previews a typed, redacted diagnostic document.
The bundle excludes credentials, SID/user/host identity, raw session/request
IDs, PIDs, creation times, paths, raw errors/events, journal/checkpoint files,
arguments and environment values. Known counts, states, units, ages, versions
and readiness/storage booleans remain to explain the problem. Unknown fields
are dropped, even when numeric or structured.

Optional `--include-report SESSION_UUID` adds the latest matching report after
the same reviewed `share`/`minimal` privacy transform used by standalone
reports. `minimal` is the default. Its JSON and offline HTML come from exactly
the same transformed dictionary. The preview contains the eligible data;
`--out PATH.zip` creates an exclusive local archive after size checks. Nothing
is uploaded. Inspect the preview, archive contents and destination before
sharing: retained numerical evidence and workload timing can still be
identifying. A support bundle is neither anonymous nor a causal diagnosis.

## Bounds and local acceptance evidence

Live freshness uses monotonic time: an active session is stale after the larger
of ten seconds or three configured sample intervals without a status/sample
advance. Offline observation age is wall-clock information and never proves a
live session. Storage inspection remains bounded by the scheduling policy.
Recovery previews never open a PID. Removal inspects at most 512 known launched
identities and 128 MiB of checkpoint files, refuses uncertainty or a live known
launch, and invokes no process-control operation.

Support previews and archive contents are limited to 16 MiB; compressed ZIPs
are limited to 8 MiB, with at most two selected reports. Archive member dates
are fixed to 2000-01-01, and member names contain no original report/session IDs. The output parent must
have a trusted local ancestor chain. A flushed private temporary file is
published by an atomic, exclusive hard link; filesystems without that operation
return an error without replacing a destination. Failed writes preserve the
previous destination and remove only this operation's own temporary file.

Native acceptance tests stop an agent with a launched and attached workload,
verify owned cleanup and the attachment's survival, inspect the offline
journal without a write, preview/apply removal, and verify every unrelated note
and evidence file remains. Another test kills an agent and recovers its intact
prefix without accessing a PID or starting a backend. Seeded string, numeric,
and structured private values are absent from both the support preview and
all JSON/HTML/archive member names; supplied numerical evidence is retained
only in documented typed fields. These are local tests, not hosted production
availability or a user pilot.

### Concurrent startup and interrupted removal

Normal startup acquires the permanent private `agent-control.lock` before it
reads or creates a credential, journal, or session. It holds this OS-owned fence
for the agent lifetime. Removal holds the same fence through every control-file
unlink. The fence file is intentionally preserved: releasing and deleting the
primary journal lock on Windows cannot admit a concurrent normal startup into
the middle of removal.

Before deleting anything, explicit removal flushes a private intent containing
the validated state identity, journal hash, and confirmed cleanup. Startup
refuses any pending removal intent before recreating credentials or adopting
state. An explicit removal retry validates that intent and the remaining
journal; a changed/corrupt authority is preserved and refused. The journal is
removed after other control files, and the removal intent is removed last.
A crash after journal removal can finish only the already-confirmed control-file
removal; it never restores process ownership. No automatic retry starts work.

The coordinated native regression pauses a separate removal process exactly
between primary-lock release and unlink, attempts a normal startup, and proves
that no credential is recreated and the journal remains unchanged. It repeats
the attempt after killing the paused remover: durable intent still blocks
startup, and an explicit retry finishes while preserving unrelated notes.
