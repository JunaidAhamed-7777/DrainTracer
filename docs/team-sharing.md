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

## Prepare the service

Install the same pinned release on the controlled host and runners. Initialize
the private store and bootstrap a project with `draintracer-team` as described
in [local team storage](team-access.md). That command remains an OS-local
administration tool; `draintracer-share` is the explicit network client and
foreground listener. Neither command is started by `draintracer`.

Use an organization-issued server certificate whose subject alternative names
match the client hostname, its private key, and the intended CA chain. Keep
these files under a trusted private local parent. Client CA files may be
publicly readable, but their contents and ancestors must not be writable by
untrusted users. The client rechecks this authority before every connection.
There is no option to disable certificate or hostname verification. A private
CA and the test certificates in this repository are different deployment
choices: repository test keys are public fixtures and must never secure a
deployment.

Run the operating exercise in a **new** private workspace, with a real named
operator and support contact. Include the transport extension:

```powershell
draintracer-ops --workspace "$HOME\draintracer-ops-new" --operator "Deployment operator" --contact "Internal support route" --with-transport --certificate "$HOME\.draintracer-tls\server.pem" --key "$HOME\.draintracer-tls\server.key" --ca "$HOME\.draintracer-tls\ca.pem"
```

The exercise certificate must cover `localhost` for its disposable loopback
probes. It measures current local role/authority/restore and TLS queue/read
behavior; retain the generated private `operating-evidence.json`. A failed
probe or budget is a failed readiness result. Declare meaningful local budgets
before running the exercise; do not relabel a failure as passing. Windows also
exercises the agent by default; Linux uses the team/local collection reference.

`serve` requires current-version private evidence no older than seven days,
passing readiness checks and transport observations. The file is an OS-local
operator gate, not signed measurement attestation or human consent. Rerun it
after a release or material deployment change. Start the foreground listener
only after this gate passes:

```powershell
draintracer-share serve --store "$HOME\.draintracer-team" --certificate "$HOME\.draintracer-tls\server.pem" --key "$HOME\.draintracer-tls\server.key" --operating-evidence "$HOME\draintracer-ops-new\operating-evidence.json"
```

The default address is `127.0.0.1:8443`. Binding another numeric address requires
`--host ADDRESS --allow-network`. The operator must configure the intended
network/firewall and process supervision separately; this command installs no
daemon, scheduled service, proxy or firewall rule. Ctrl+C closes the listener.
TLS uses the fixed `draintracer-team/1` ALPN protocol before transmitting any
credential; an ordinary HTTPS endpoint cannot receive the credential frame.

## Preview, queue and retrieve a report

Issue separate role credentials through local administration or an existing
remote administrator. `issue-credential ROLE --output NEW_PRIVATE_FILE` writes
the new bearer secret exclusively and prints metadata only. Deliver that file
through an independently controlled channel. If delivery fails, inspect the
credential list and revoke any uncertain key; a failed delivery attempts
revocation but cannot promise that an unavailable server acknowledged it.

Global client options precede the subcommand. A hostname other than localhost
also requires `--connect-ip NUMERIC_ADDRESS`: the certificate still verifies
against the hostname, and no implicit DNS lookup or proxy is used. For example:

```powershell
draintracer-share preview .\run.json --profile minimal --output "$HOME\.draintracer-share\preview.json"
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\operator.json" --ca "$HOME\.draintracer-tls\ca.pem" enqueue-report --queue "$HOME\.draintracer-share\queue" .\run.json --profile minimal
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\operator.json" --ca "$HOME\.draintracer-tls\ca.pem" queue --queue "$HOME\.draintracer-share\queue"
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\operator.json" --ca "$HOME\.draintracer-tls\ca.pem" flush --queue "$HOME\.draintracer-share\queue" --limit 8 --deadline-seconds 60
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\viewer.json" --ca "$HOME\.draintracer-tls\ca.pem" history
```

Create only the private leaf beneath a trusted existing parent. Output files
must be new; preview/export never overwrite an existing file. Preview and
enqueue are offline: they apply the selected privacy transformation before
persisting queue payloads. Queue inspection reads private local state. Only
`flush` sends those entries; the server applies its own project privacy policy
again. Review the decoded preview before sharing. Privacy transformation is a
bounded allowlist, not a guarantee that every application-specific value is
non-sensitive.

An entry expires within one day and has at most twenty attempts. Each flush
handles at most eight entries within a maximum sixty-second budget. Transient
failures retain the same UUID and payload for a later explicit flush;
terminal authorization/input failures become blocked. Inspect and correct the
cause before `--retry-blocked`. A full queue refuses admission. Queue/status or
flush returns `2` while entries remain; inspect the JSON state rather than
treating that code as a local monitoring failure. History has a bounded limit
and continuation cursor. `export REPORT_ID --output NEW_PRIVATE_JSON` retrieves
sanitized JSON; no command opens uploaded HTML.

## Approve and share a controlled comparison

First complete a real [controlled baseline promotion](controlled-baselines.md)
with at least two independent eligible private runs. Keep that W4 store on the
controlled runner. An administrator can publish its validated revision and
audit head, using a new UUID request ID for this intended approval and the same
ID if its acknowledgement is uncertain:

```powershell
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\admin.json" --ca "$HOME\.draintracer-tls\ca.pem" approve-baseline .\approved-baseline --repository owner/repository --request-id NEW_UUID_HEX
```

Record the returned `record_id` as the approval ID. Replace `FULL_SOURCE_SHA`
below with the actual full source revision and `APPROVAL_ID` with that ID:

```powershell
draintracer-share prepare-comparison .\approved-baseline --run .\after-1.json --run .\after-2.json --source-commit FULL_SOURCE_SHA --repository owner/repository --approval-id APPROVAL_ID --output "$HOME\.draintracer-share\receipt.json"
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\operator.json" --ca "$HOME\.draintracer-tls\ca.pem" enqueue-comparison --queue "$HOME\.draintracer-share\queue" "$HOME\.draintracer-share\receipt.json"
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\operator.json" --ca "$HOME\.draintracer-tls\ca.pem" flush --queue "$HOME\.draintracer-share\queue"
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\viewer.json" --ca "$HOME\.draintracer-tls\ca.pem" history --repository owner/repository --source-commit FULL_SOURCE_SHA --workload-id WORKLOAD_ID
draintracer-share --endpoint tls://localhost:8443 --credentials "$HOME\.draintracer-share\viewer.json" --ca "$HOME\.draintracer-tls\ca.pem" comparison RECEIPT_RECORD_ID --text
```

`prepare-comparison` performs the actual strict W4 check offline and preserves
its policy exit code. A written regression/inconclusive receipt is evidence of
that outcome, not success. It projects only fixed typed metrics and generic
qualifications, excluding private report paths, arbitrary caveat text and
selected environment values. Use `--report-id` on enqueue to link optional
already-uploaded project evidence. The server binds the receipt to the live
approval's project, repository, workload, revision and audit head; it does not
recompute private W4 measurements. A distinct authorized viewer reads the same
stored receipt as JSON or literal plain text. Scoped exceptions, budget
breaches and unknowns remain qualified. No probability or allocation cause is
inferred from the displayed observed-run values.

Project administrators can revoke credentials, delete reports/records, inspect
bounded audit metadata, request sanitized backups and restore eligible
evidence. Approval/evidence deletion invalidates linked receipts; backups do
not roll credentials or deletion authority backward. Apply the retention and
authority-loss procedures in the [operating runbook](service-operations.md).
External exports and copies need their own operator-controlled retention.
