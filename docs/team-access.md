# Local team storage and access boundary

The team core is opt-in storage and authorization. It starts no listener, serves
no HTML, creates no public links and performs no federation. The OS administrator
and owner of its private directory are trusted. Bearer callers, report bytes,
report claims and cross-project identifiers are untrusted. A transport must
authenticate each request and expose only project operations; OS-local project
bootstrap and global administration must never become remote endpoints.

`draintracer.team_store` provides `TeamStore.create(directory, limits=...)` and
`TeamStore.open(directory)`, both context managers. Closing prevents new
operations; an already running transaction may finish. It has no background
thread to join. Opening an absent, damaged or unsupported authority
database fails closed. It never silently creates replacement permissions.
Windows storage uses the reviewed local/trusted ancestry checks and a protected
current-user/SYSTEM private directory; inherited SQLite data may be owned by
the current SID, SYSTEM, Administrators or TrustedInstaller, whose privileges
are already outside this boundary. Their DACL still permits only the current
SID and those explicitly privileged principals; public read access is refused.
Protected directories and credential files keep exact current-SID ownership.
Fresh credentials receive that protected policy at creation; existing files are
never repaired. POSIX storage requires current-UID ownership, a private
0700 directory and 0600 files. Symlinks, reparse points, remote Windows paths and
untrusted writable ancestors are refused. No physical-wipe or OS-admin isolation
guarantee is implied.

Local `bootstrap_project(name, policy=..., credential_ttl=...)` creates a project
and its first credential. `authenticate(token, project_id)` returns an opaque
`Actor`, not caller-provided JSON fields. No transport may deserialize an Actor
or accept a supplied role as authority. Tokens have 256 random secret bits;
only their digests are stored. An Actor carries no enduring authorization:
every operation rechecks project existence, credential expiry/revocation and
current role inside its database transaction. Parsing an upload also has an
early authorization check, followed by another check at commit.

Roles are `viewer` (list/read/export reports and read policy), `operator`
(viewer plus ingest) and `admin` (operator plus deletion, policy, project
credentials, audit and managed backups). An expired or revoked credential cannot
regain authority through a cached Actor or a report backup. Credentials are
returned only once to their authorized issuer. Local credential JSON must be
written exclusively to a private file; secrets never appear in audit records,
ordinary command arguments or diagnostics.

Project methods are `whoami`, `ingest(actor, raw: bytes)`,
`list_reports(actor, limit=100, cursor=None)`, `read_report(actor, report_id)`,
`export_report(actor, report_id) -> bytes`, `delete_report`,
`get_policy`/`update_policy`, `issue_credential`/`list_credentials`/
`revoke_credential`, `audit(actor, after=0, limit=100)`, `backup`, `list_backups`,
`delete_backup`, `restore` and `purge`. Responses use `team_schema: 1` and
server-generated store/credential/report/record UUID identifiers (32 lowercase
hex characters). Source report IDs remain
untrusted report data and must never become filenames or authority selectors.
Responses contain sanitized report data, stable
machine summaries or operation metadata, never server filesystem paths. Errors
have a safe `TeamError.code`: `unauthorized`, `forbidden`, `not_found`, `invalid`,
`quota`, `conflict`, `busy` or `storage`. A transport maps these codes to its own protocol.

Ingestion bounds raw bytes before decoding, uses the public report contract's
in-memory parser, and always applies the project's `share` or `minimal` privacy
profile on the server before persistence. A client privacy claim never skips
this transformation. Stored reports remain untrusted diagnostic evidence, not
proof of a cause. The core accepts and returns JSON only. A caller must not
render uploaded HTML or concatenate report strings into authenticated HTML.

Default ceilings are 8 MiB per upload, 64 MiB per project's stored reports and
backups, 256 MiB global logical data, 32 projects, 32 active credentials per
project, 100 reports and three managed backups per project. Report retention is
seven days by default. Admission, expiry and quota accounting are transactional.
Audit content is bounded by row/byte retention with explicit dropped counts;
records contain IDs/actions/results rather than secrets or report text. SQLite
uses a finite two-second busy timeout, EXTRA synchronous commits and DELETE
journaling. The database is capped at 512 MiB; rollback overhead is bounded by
that database cap, WAL is disabled and temporary tables stay in memory. Logical
quotas do not pretend that SQLite page/journal overhead is zero.

Managed backups are explicitly requested sanitized-report snapshots. They
contain no credentials, policy or audit rollback. They expire within 24 hours
and no later than their original report expiry. Report/project deletion
invalidates containing backups transactionally. Restore requires current live
project-admin authority, honors deletion/expiry metadata and rechecks current
quotas; it never restores permissions. These snapshots support logical report
recovery while authoritative metadata exists, not whole-database-loss disaster
recovery. Missing authority or tombstone state fails closed. Unmanaged exports
and external filesystem backups are outside enforced deletion: operators must
apply their own documented expiry and access policy to those copies.

Verification must cover role/project isolation, cached-Actor revocation,
expiry, malformed/oversized ingestion, server-side privacy, concurrent quota
admission, SQLite/sidecar privacy, interrupted transactions, deletion and backup
invalidation, expired restore refusal, bounded audit history and authority-loss
failure. Actual local commands and tests exercise these boundaries. A hosted SLA, public
sharing site or third-party identity provider is not part of this local core.


## Explicit local commands

A new store's parent must already exist with trusted permissions. This Windows
example creates only the private leaf. The same commands work on Linux with a
private owned parent, `Store="$HOME/.draintracer-team"`, and normal shell paths.
No command starts a listener or opens an uploaded HTML document.

```powershell
$Store = "$HOME\.draintracer-team"
draintracer-team --store $Store init
draintracer-team --store $Store bootstrap "performance" --credential-output "$Store\admin.json"
draintracer-team --store $Store --credentials "$Store\admin.json" whoami
draintracer-team --store $Store --credentials "$Store\admin.json" ingest .\run.json --request-id 0123456789abcdef0123456789abcdef
draintracer-team --store $Store --credentials "$Store\admin.json" reports
draintracer-team --store $Store --credentials "$Store\admin.json" backup
```

Use a new random UUID hex for each intended ingestion and reuse that exact ID
and bytes for an uncertain retry. `ingest(..., request_id=...)` returns flat
metadata including `report_id`, `request_id`, `replayed`, a sanitized payload
hash, byte count, expiry, privacy profile and machine summary. Identical retries
return the same report; changed bytes or operation conflict. Deleted/expired
results cannot be resurrected. The shared report/record retry ledger retains
input hashes and tombstones for 30 days, the maximum credential/report lifetime;
its default cap is 4,096 entries per project. Full live retry history is never
evicted to admit a new key. Old queues must expire within that horizon.

`credential-issue viewer --credential-output <new-private-file>` creates a
separate role-scoped credential; `credentials` lists metadata and
`credential-revoke <id>` invalidates it immediately, including cached Actors.
A credential is written exclusively, flushed and never printed. A delivery
failure attempts audited revocation of that new key; an uncertain revocation
requires local inspection. The partial file is left for inspection and a retry
cannot overwrite it. Deliver authorized credential files to recipients through
an independently controlled channel. Do not put bearer values in arguments,
logs, reports or source control.

`policy-set <json>` accepts exactly the `ProjectPolicy` fields. Lower retention
never extends existing evidence. Tightening `share` to `minimal` transforms
existing reports and invalidates older snapshots. Lower quotas require existing
evidence to fit; admission does not silently evict live reports. `export <id>
--output <new-private-json>` writes sanitized JSON exclusively. `report-delete`
and `project-delete` remove evidence, linked records and snapshots atomically;
project deletion revokes every key and leaves only bounded deletion/audit
metadata. `admin-recover <project-id> --credential-output <new-file>` is an
explicit OS-local recovery that revokes prior keys, never recreates a deleted
project and cannot be invoked remotely.

`purge` expires the current project's data. Schedule the OS-local `purge-local`
command to expire inactive projects as well; retention is enforced on each
project access, and the local maintenance command makes timely deletion possible
without live user traffic. This process must run for wall-clock retention to be
physically enforced while the application is idle. SQLite free pages may remain
allocated up to the physical cap. Project deletion history is bounded to 1,024
project identities, and report deletion history to the configured retry cap;
capacity exhaustion requires expiry/inspection, never silent history loss.

## Explicit baseline approvals and receipts

The protected controlled-runner baseline store remains the source of W4
comparisons. Privacy-transformed team reports lack private provenance required
for strict local comparisons. The team core never strips privacy markers or
claims that a receipt is remote attestation of source code.

`create_record(actor, kind, value, key=None, report_ids=(), ttl_seconds=None,
request_id=None)` accepts only strict `baseline_approval` or
`comparison_receipt` DTOs. Approvals require admin role. Operators may create
receipts referencing a live same-project approval with the same repository,
workload ID, integer baseline revision and audit head. Every receipt says
`basis: "client-computed"`; its outcome and comparison digest remain the
client's recorded claim. Receipt expiry cannot exceed approval or linked report
expiry. Deleting an approval invalidates its receipts; deleting linked evidence
invalidates the record and its retry result.

The optional `key` is a bounded logical history label, not an authorization key.
Record retries use the shared `request_id` ledger and bind the full typed value,
kind, key, sorted report IDs and requested TTL. `list_records` supports bounded
pages and exact kind/key filters; authorized clients can filter these typed
values further by repository, source commit and workload. Notes and
qualifications are deliberately supplied governance text: review them before
sharing. Unknown fields, arbitrary report fragments, paths injected into typed
metric structures, controls, nonfinite numbers and excessive structures fail
closed. This is JSON storage, never an authenticated HTML rendering surface.

The database validates its actual table/index/constraint shape when opened and
on every connection, not just its declared version. Logical admission checks
stored byte counters against actual payload lengths; damaged authority fails
closed. Managed restore can repair a payload only while its live report
identity, hash, expiry and deletion metadata remain authoritative.

SQLite's [`EXTRA` synchronous policy](https://www.sqlite.org/pragma.html#pragma_synchronous)
adds directory synchronization after rollback-journal unlink in DELETE mode.
This is an explicit durability configuration, not a claim of having tested a
physical power-loss event. The [SQLite page ceiling](https://www.sqlite.org/pragma.html#pragma_max_page_count)
bounds database growth separately from logical report quotas.
