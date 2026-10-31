# Local reference operating objectives and recovery

This is a measured local reference deployment for the optional agent and team
store. It is not a hosted production baseline, uptime commitment, staffed
on-call service or response-time guarantee. Synthetic fault drills do not
constitute a human pilot or prove long-term availability.

## Ownership and pilot gate

The reference implementation/release maintainer is
[JunaidAhamed-7777](https://github.com/JunaidAhamed-7777), the repository owner.
A deployed instance must separately record a named operator, support contact,
current version/configuration, private backup location, and recovery runbook.
The operator owns disk/access monitoring, credential issuance/revocation,
retention decisions, upgrades and incident recovery. Public repository issues
are for sanitized reproducible defects; use the [security reporting route](../SECURITY.md)
for sensitive failures. No maintainer response time is promised.

Before an external pilot, the deployment record must identify that operator
and contact, point to this runbook plus its local configuration, and contain a
successful recent restore exercise. Missing ownership, authority metadata, or
restore evidence blocks readiness. This gate records operational prerequisites;
it does not manufacture participant consent or pilot results.

## What the reference exercise measures

A bounded disposable fixture records monotonic local timings and explicit
sample counts for agent freshness/readiness, accepted/completed runs, committed
report durability after reopening storage, ingestion/upload commit latency,
authorized-read probe success, and driver/agent CPU and sampled RSS. Rates
state their numerator/denominator; zero observations are unknown. Sparse
samples report observed minimum/median/maximum rather than an unsupported
long-term percentile or SLA. Sampled RSS is a lower bound on true peak memory.

Each deployment declares its own local acceptance budgets. Reference defaults
are a two-second freshness/ingestion/readiness budget, complete cleanup/report
persistence for accepted fixture runs, successful authorized baseline probes,
and a 256 MiB sampled-RSS budget per reference process. These are acceptance
checks on the recorded machine and workload, not universal performance claims.
The report distinguishes baseline availability probes from expected refusals
and deliberately injected outages. It records partial or failed exercises
rather than silently discarding them.

## Failure and restore invariants

The team store's live authoritative credential, permission, policy, expiry and
deletion metadata remains the source of authority. Sanitized logical evidence
backups contain no credential or policy rollback. A revoked/expired credential
cannot regain access after restore. Deleted or expired evidence cannot be
resurrected from an older snapshot. Missing, corrupt, future-version or
unrelated authoritative metadata blocks restoration and new access. Do not
replace a missing authority database with an older whole-database copy.

Fault drills detect malformed uploads, forbidden/revoked/expired credentials,
storage/write interruption, lost authority and unavailable endpoints. A valid
current admin restores eligible sanitized evidence only through the store's
supported restore operation. Verify hashes, privacy transforms, expiry,
permissions, deletion invalidation and an audit entry after restoration. An
expected refusal is a successful safety check, not a successful data restore.

## Storage and access incident runbook

1. Stop new ingestion/launch admission. Preserve the private state, intact
   journal/checkpoints, current authority metadata and bounded diagnostics.
2. Inspect free space, storage admission limits, file ownership/ACLs, version,
   lock/busy errors, freshness and accepted/completed counts. Do not widen
   permissions, delete arbitrary files or retry uncertain process control.
3. Cancel through the authenticated agent and confirm ownership-specific
   cleanup. Attachments remain untouched; resolve any known owned survivor
   before removal. Recover intact prefixes as partial evidence when needed.
4. Revoke a compromised credential through current authority. Do not copy
   credentials, raw errors, identities or private evidence into support posts.
   Use the reviewed support preview and an authorized destination.
5. Resolve capacity/access and run the bounded health/restore exercise in a
   disposable private fixture. Resume admission only after the declared checks
   pass; document unresolved gaps and operator decisions.

## Upgrade, migration, rollback and outage runbook

Stop admission and the agent, verify stopped writer/cleanup state, and preserve
current authoritative metadata separately from sanitized evidence snapshots.
Install a pinned new release in a separate private environment. Read its
schema/compatibility requirements; a newer unsupported schema must fail closed.
Exercise supported migration and restoration on a disposable copy before
pointing the new process at retained state. No command should rewrite future
state merely to make an older executable accept it.

Rollback chooses a compatible prior executable and the current authoritative
permissions/deletion state. An older sanitized evidence snapshot may be
restored only when current authority still permits every included record and
its original expiry remains valid. If that cannot be shown, keep service
unavailable and recover through an explicitly reviewed authority process;
never revive an old token or deleted evidence to improve apparent availability.
An endpoint outage is recorded separately from a leak verdict and does not
authorize another launch. Preserve partial evidence, diagnose locally and
verify recovery before resuming clients.

## Run the bounded reference exercise

Install the current package and choose a **new** workspace.
The command refuses every existing workspace, including an empty directory.
It never accepts a live deployment store, modifies supplied key/certificate
files, starts a remote target, or contacts a service outside loopback.

```powershell
draintracer-ops --workspace "$HOME\draintracer-ops-fixture" --operator "Named operator" --contact "Support contact"
```

The default baseline imports no network transport and needs no TLS files.
Windows defaults to local authority, collection and agent exercises; Linux
defaults to local authority and collection. Use
`--components team` for a deliberately team-only reference. The agent case
starts a distinct foreground process and owns every workload it launches.
Use a controlled account and private/trusted ancestors, as for the agent
itself. The fixture remains for local inspection; it contains private
authority data and intentionally damaged/missing-authority
examples. Keep it private and remove it only after inspecting the evidence.
Do not upload the workspace as a support attachment.

The command prints one JSON object with the private evidence path and
`operating_prerequisites_met`; exit 0 means the requested disposable reference
checks passed, and exit 2 means a failed check or unusable configuration.
`operating-evidence.json` retains unsuccessful/partial probes without raw
exceptions or credentials. Missing samples, restore evidence, requested
components, operator/contact or passing budgets block the readiness result.
A disk failure can prevent creating the artifact; the command then returns a
fixed error instead of reporting success. Existing results are never replaced.

Reference defaults can be declared explicitly through `--readiness-seconds`,
`--freshness-seconds`, `--upload-seconds`, `--read-seconds`,
`--cleanup-seconds` and `--sampled-rss-mib`. The evidence records the actual
budgets, package/dependency versions, component scope and UTC exercise dates.
Every duration budget is finite and at most 60 seconds; memory is capped at
4 GiB. Resource samples run every 20 ms for at most two minutes, plus a final
sample. CPU is observed process CPU seconds, not a machine-normalized CPU
percentage. RSS remains a sampled lower bound.

## What passing evidence establishes

- Five authorized store reads have explicit success/attempt counts.
  Expected role and expiry refusals are
  separate probes, excluded from baseline availability numerators.
- Direct sanitized report ingestion measures local upload/commit latency.
  Reopening storage must preserve the committed bytes and their hash. A real
  local monitor must complete with cleanup and readable JSON/HTML on both
  supported platforms, independently of any sharing listener.
- Windows measures two baseline accepted/completed runs: natural exit and
  cancellation. Both require three live samples before the driver continues,
  complete cleanup and readable JSON/HTML. The natural fixture waits for a
  private release marker, then performs its 1.3-second work and exits normally;
  its gate is bounded at 30 seconds. The existing 10-second sample wait,
  45-second target limit and measured operating budgets remain unchanged.
  Completed-state age is never substituted for live freshness. A third
  deliberately crashed run is separate from that completion-rate denominator;
  its owned job must stop, and offline recovery must remain partial and
  unconfirmed through the saved cutoff. Recovery never restarts the agent.
- A damaged report payload must be detected, restored by a current admin,
  and readable with the same hash/original expiry by two technical credentials.
  A viewer cannot restore; a revoked cached actor stays revoked. Deletion
  invalidates old snapshots/retries, stricter privacy invalidates an older
  snapshot, and restore is audited. Report/snapshot retention and cached-credential
  expiry use separate disposable stores with explicitly stepped fixture clocks.
  The credential drill commits and authenticates a credential, confirms its
  protected read, then advances past its returned expiry and requires the same
  cached actor to be refused. This tests the authority invariant without
  measuring elapsed credential-expiry latency. Baseline readiness, read,
  ingestion, cleanup and resource measurements use real clocks.
- Missing/corrupt/future/unrelated authority fails closed. A missing current
  database is never recreated by restore. An interrupted SQL transaction must
  roll back and reopen under the exact retained authority.

## Optional transport extension

After the optional sharing transport is available, explicitly add
`--with-transport --certificate PRIVATE_CERT --key PRIVATE_KEY --ca PRIVATE_CA`.
The certificate must identify `localhost`; all supplied files remain private
and unchanged. This extension lazily imports the sharing modules and starts
only a disposable loopback listener. It adds actual TLS upload latency, three
authorized TLS export probes, durable queue replay after a simulated lost
acknowledgement, detected listener outage, local collection during that outage,
and explicit listener restart/queue recovery. These additional observations
become mandatory readiness gates only when the extension is selected.
The TLS service runs in the driver process, so its CPU/RSS is included there.
Transport fixture credential files remain private in the retained workspace.

The output is recent local exercise evidence for the named operator to review.
It does not establish the actual deployment's disk durability under power loss,
off-host authority disaster recovery, long-term uptime, a staffed incident
response, human consent or pilot completion. The operator must separately
verify the live deployment's current configuration, owner/contact, private
authoritative-state protection and this runbook before an external pilot.
Do not carry a passing old fixture artifact forward as authorization to ignore
a changed release, permission policy, expiry/deletion state or failed restore.
