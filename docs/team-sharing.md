# Optional sharing on an organization-controlled host

Sharing never starts as a side effect of monitoring. A foreground service and
an explicit client command exchange length-prefixed JSON over authenticated
TLS. The listener defaults to loopback. Deployment on a controlled network
requires an operator, a server certificate and private key, a trusted CA, and
the operating checks in [service operations](service-operations.md). This
reference implementation provides no hosted service or availability promise.

The server uses the [team access boundary](team-access.md): project-scoped,
revocable credentials; transactional role checks; bounded ingestion; server-side
privacy transformation; quotas; retention; and deletion. No uploaded HTML,
anonymous links, remote program execution, local baseline store path, or global
bootstrap operation is exposed. TLS certificate and hostname verification are
mandatory. There is no cleartext fallback, redirect, or proxy discovery.

The transport bounds four simultaneous handshake/request workers, one request
per connection, a 16 MiB frame, JSON depth/value counts, and socket deadlines.
Client credentials are read from private files, never command arguments. Errors
return fixed codes instead of exception text, paths, credentials, or report
content. Server authority remains the live team database.

## Baselines and comparisons

Strict W4 promotion/checking runs on the controlled runner against its private
evidence. Privacy-transformed reports deliberately cannot establish strict
provenance equivalence. The service never removes that qualification.

A project administrator records an approved W4 revision and audit head for a
repository/workload. An operator can submit a typed comparison receipt linked
to that approval and optional sanitized reports. Another authorized viewer can
retrieve the same receipt and evidence. Receipts say **client-computed**:
authorization establishes who submitted a record, not source attestation or
independent server verification of the private measurements. Approval expiry,
revocation and evidence deletion are checked by the authoritative store.

## Outages and retries

An explicit private local queue accepts at most 64 entries and 32 MiB. It has
one cooperative writer, atomic state replacement, finite retry deadlines,
exponential backoff with jitter, and stable UUID/payload identities. Replaying
the same accepted upload returns one logical report. Changed payloads under an
existing key conflict; deletion does not authorize resurrection. Queue pressure
and terminal authorization/input failures are visible to the caller.

Collection, local JSON/HTML export and process cleanup never import the
transport or wait for this queue. Enqueue/flush failures retain local reports.
Only the explicit flush command attempts the network. Operators review queued
content and retention before sharing; credentials are never stored in entries.

Verification must include two distinct project credentials reading one approved
comparison, cross-project and revoked-token refusals, certificate/hostname
refusals, uncertain-reply deduplication, durable bounded retries, outage-local
collection independence, malformed frames, and bounded connection exhaustion.
Two technical test credentials do not constitute a consent-based human pilot.
