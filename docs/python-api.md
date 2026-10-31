# Supported Python session API

The supported integration boundary is `draintracer.api`. It does not import or
initialize the dashboard, file picker, keyboard input, or update checker.
The report reader remains available from `draintracer.report_contract`.

After `pip install draintracer`, run the [standalone example](../examples/python_session.py)
outside the repository: `python python_session.py /path/to/worker.py`. It uses
only documented supported imports and produces the normal JSON/HTML pair.

```python
from draintracer.api import Session, SessionOptions

options = SessionOptions(timeout_seconds=30)
with Session.launch("worker.py", args=("--items", "1000"), options=options) as session:
    result = session.wait()
print(result.measurement, result.cleanup, result.exit_code)
```

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
Commands require a running session. After completion, use `report()` for the
final report and `result.exports` for the pair written by `export_on_close`
(true by default). Reports and paths can contain sensitive local information;
use the report command's privacy preview before sharing them.

`SessionResult.reason` distinguishes `target_exited`, `timeout`, `cancelled`,
and `failed`. Startup, cleanup, measurement, coverage and errors are separate
fields. `exit_code` uses the CLI's policy codes: explicit API cancellation is
130 when no signal has already failed; startup/runtime/export failures are 2, budgets 5, requested target failures
4, requested coverage/inconclusive failures 3, signals at `fail_on` 1, otherwise
0. A timeout requests normal cleanup and is not by itself a leak or failure.
An incomplete cleanup remains explicit in `cleanup` and the report, even when
default policies otherwise return zero. `close()` before `start()` completes
as cancelled without launching or attaching to anything.
The shared precedence is startup failure, then signal, cancellation,
runtime/export failure, budget, requested target failure, requested coverage or
inconclusive failure, then pass. A recorded signal remains a failure even if a
later cancellation or monitor problem ends the session.

The API does not install signal handlers. Context management handles a Python
exception or KeyboardInterrupt by requesting normal cleanup. An abrupt monitor
process kill cannot run context exit; optional checkpoints preserve an evidence
prefix under the recovery contract, without claiming final cleanup succeeded.

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
