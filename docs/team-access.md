# Local team storage and access boundary

The team core is opt-in storage and authorization. It starts no listener, serves
no HTML, creates no public links and performs no federation. The OS administrator
and owner of its private directory are trusted. Bearer callers, report bytes,
report claims and cross-project identifiers are untrusted. A transport must
authenticate each request and expose only project operations; OS-local project
bootstrap and global administration must never become remote endpoints.

`draintracer.team_store` provides `TeamStore.create(directory, limits=...)` and
`TeamStore.open(directory)`. Opening an absent, damaged or unsupported authority
database fails closed. It never silently creates replacement permissions.
Windows storage uses the reviewed local/trusted ancestry checks and a protected
current-user/SYSTEM private directory; inherited SQLite files must also pass a
confidentiality check. POSIX storage requires current-UID ownership, a private
0700 directory and 0600 files. Symlinks, reparse points, remote Windows paths and
untrusted writable ancestors are refused. No physical-wipe or OS-admin isolation
guarantee is implied.

Local `bootstrap_project(name, policy=..., credential_ttl=...)` creates a project
and its first credential. `authenticate(token, project_id)` returns an opaque
`Actor`, not caller-provided JSON fields. Tokens have 256 random secret bits;
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
server-generated store/credential UUID identifiers. Source report IDs remain
untrusted report data and must never become filenames or authority selectors.
Responses contain sanitized report data, stable
machine summaries or operation metadata, never server filesystem paths. Errors
have a safe `TeamError.code`: `unauthorized`, `forbidden`, `not_found`, `invalid`,
`quota`, `busy` or `storage`. A transport maps these codes to its own protocol.

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
uses a finite two-second busy timeout, FULL synchronous commits and DELETE
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
failure. A hosted SLA, public sharing site or third-party identity provider is
not part of this local core.
