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
