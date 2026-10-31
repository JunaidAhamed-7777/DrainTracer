# Supported Python session API

The supported integration boundary is `draintracer.api`. It does not import or
initialize the dashboard, file picker, keyboard input, or update checker.
The report reader remains available from `draintracer.report_contract`.

`Session.launch(path, args=(), options=SessionOptions())` and
`Session.attach(pid, create_time=..., options=SessionOptions())` construct a
single-use session. Construction does not start or control a process. `start()`
returns after starting a controller thread; `wait(timeout)` returns a
`SessionResult`, or `None` while work continues. `cancel()` requests normal
ownership cleanup. `close(timeout=10)` requests cancellation and waits up to the
given deadline. A timeout is explicit: cleanup may still be running and must not
be reported as complete. Context exit calls `close()` and raises `TimeoutError`
if it cannot confirm completion. Attached targets are detached; launched trees
are terminated under the same ownership rules as the CLI.

Options use seconds, milliseconds, bytes, and counts explicitly. They are
validated before starting. No configuration discovery or global signal handler
is installed. Attach creation time is checked before sampling when supplied;
omitting it means attaching to the identity found at startup.

`status`, `snapshot`, and final result records are immutable detached values.
`report(timeout)` returns an independent mutable report dictionary. `export()`
writes the usual JSON/HTML pair only under the configured report directory and
returns an `ExportResult`; an HTML failure is explicit even when JSON succeeds.
Exports and commands run on the controller, never on the sampling thread.

Pause/resume and kill require separate `allow_pause` and `allow_kill` options,
both false by default. Commands use a bounded controller queue. Queue saturation
raises `RuntimeError`. A command deadline expiring before execution cancels it;
if already executing, `CommandTimeoutError.may_have_run` is true. The caller
must inspect subsequent status rather than assume it did not execute.

Subscribers receive immutable `SessionEvent` records. Each subscription retains
at most 64 pending events and reports its drop/failure counts. A shared pool has
four daemon workers and at most 256 scheduled subscriptions; saturation drops
notifications, never samples or exact session facts. A subscription runs at
most one callback at a time. Exceptions are counted and isolated. Unsubscribe
and cleanup never join user callbacks; a callback already running may finish
after unsubscribe. Four permanently blocked callbacks exhaust notification
delivery, but monitoring, cancellation, and cleanup continue. Callbacks must
not assume every sample is delivered. Poll the status or final report for exact
facts.

Compatibility follows the project's Semantic Versioning policy. Documented
imports, option meanings, and result fields are supported within a major
version; additive options/fields/events may appear in minor releases. Internal
modules, names beginning with `_`, callback scheduling order across sessions,
and exact diagnostic prose are not part of the contract. Callers should handle
unknown future event kinds and use report schema validation for stored data.
