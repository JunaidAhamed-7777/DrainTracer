# Reproducible workload recipes

`draintracer-workload recipe.json` runs a headless monitoring session with
explicit launch choices, optional readiness and exercise steps, and optional
bounded output files. Use the same recipe in a developer terminal and CI.
Install dependencies first; the command does not perform the interactive
release check. From a source checkout, use `python -m src.workload` instead.

Recipes are UTF-8 JSON, limited to 64 KiB. Unknown or duplicate options fail
before launch. Paths resolve relative to the recipe file; `cwd` defaults to
its directory. Commands contain literal argument arrays. No shell expansion,
variable substitution, or interactive target input is provided. Windows batch
targets retain the monitor's literal `cmd.exe` argument forwarding.

```json
{
  "schema": 1,
  "command": {
    "target": "server.py",
    "interpreter": "C:/Python314/python.exe",
    "args": ["--ready-file", "run-104.ready"]
  },
  "cwd": ".",
  "inherit_environment": false,
  "environment": {
    "MODE": "benchmark",
    "API_TOKEN": {"from_env": "CI_WORKLOAD_TOKEN"}
  },
  "config": ".drainrc.toml",
  "profile": "ci",
  "timeout": 90,
  "readiness": {"file": "run-104.ready", "timeout": 15},
  "exercise": [
    {"target": "exercise.py", "args": ["--requests", "100"], "timeout": 30, "expect_exit": 0}
  ],
  "capture_logs": {"max_bytes": 65536}
}
```

Replace the paths and commands with your own disposable workload. Omit
`interpreter` for automatic Python execution with DrainTracer's interpreter,
or for a native executable or Windows batch launcher. An explicit interpreter
receives the target path followed by the literal arguments. The interpreter
must be an existing file; an unavailable executable fails as a startup error.

An explicit `config` uses the normal TOML validator, with an optional `profile`.
The recipe's `timeout` is always the session limit, overriding a TOML timeout.
No ambient configuration is discovered. Without `config`, monitoring uses
built-in defaults. `--report-dir PATH` overrides the configuration's report
destination. Use `--no-color` to suppress ANSI colors.

## Environment and provenance

By default the target and exercise processes receive only the selected
environment plus Windows system-location and temporary-directory variables
when present. Set `inherit_environment` to `true` only when the workload needs
the complete parent environment. There are at most 64 selected variables, each
with a string value of at most 4096 characters. A `from_env` source must exist;
missing sources fail before launch. Values are never printed, fingerprinted,
or embedded as environment fields in reports.

The report's `workload` section records target/interpreter/cwd paths, argument
counts and fingerprints, selected variable **names** and source kinds, time
limits, exercise outcomes, and log-file metadata. Paths and names can still
identify a project. The recipe fingerprint excludes environment values and
cannot prove that environments match. Ordinary report settings retain the
resolved sampling and outcome policies. Review [privacy guidance](privacy.md)
before publishing any evidence.

Comparisons flag differing or missing recipe execution choices and unfinished
workflows. Because environment values are deliberately absent, even matching
recipes receive a warning and strict comparisons withhold an equivalence
verdict. An ordinary comparison still shows measured differences with that
qualification; independently verify the private environments before drawing
conclusions. A recipe fingerprint does not override this limit.

## Readiness, ownership, and exits

Readiness waits for a new regular file created by your workload. Choose a fresh
marker for every run; an existing file or dangling link is refused, including
a recheck immediately before launch. Its parent must already exist. The
monitor never removes the marker or trusts a stale marker as readiness. This
is a synchronization convention for a trusted local workload, not proof of
which process created the file.

The readiness deadline is at most 300 seconds. Up to eight exercise commands
run sequentially after readiness while resource sampling continues. Each has
its own deadline (at most one hour) and expected exit code (default `0`).
The session deadline begins when the target launches and covers readiness,
exercises, and later monitoring; its limit is at most 24 hours. All durations
in JSON are positive seconds. Operating-system process creation is synchronous.

The target and every exercise command are launched and tracked as separate
owned process trees. A failed step, timeout, Ctrl+C, or monitor error attempts
to stop those trees; unrelated processes are left running. No attach or
external process-control mode exists in this command. Target stdin is closed;
exercise processes cannot consume the monitor's keyboard input.

Readiness or exercise failure returns **6**, before any leak/budget verdict,
and retained evidence is exported with a failed workload outcome. Ctrl+C
returns **130**. Invalid recipes and target startup errors return **2**.
HTML keeps the failed/interrupted workflow above the resource evidence rather
than presenting a completed-run verdict. The typed `outcome.workload.state`
also survives privacy transformation and appears as `workload_state` in the
machine summary; raw recipe provenance and log-file paths are removed there.
Once the workflow completes, ordinary headless leak, budget, target-error,
and inconclusive policies apply. A short successful recipe can still lack a
judged leak window; choose a sufficiently long timeout and strict CI policies.

## Optional output files

Without `capture_logs`, target and exercise stdout/stderr are discarded.
Opt-in capture writes separate files under a unique `workload-logs-*` directory
beside reports. Each stream has a bound from 1024 bytes to 1 MiB. The reader
continues draining after the bound so a full log does not block the target.
Complete UTF-8 lines are redacted before writing; a line over 8192 bytes is
omitted entirely, and a line that exceeds the remaining bound is not partially
written. Metadata reports truncation, omitted lines, and output errors.

Selected environment values and recognized credential patterns are removed.
This does **not** guarantee secret-free logs: inherited variables, transformed
secrets, unusual formats, paths, and arbitrary application output can remain.
Disable capture for sensitive targets. Logs are separate artifacts and never
embedded in JSON or HTML; inspect their permissions, content, publication, and
retention independently. Never publish the recipe itself when it has literal
secret values.

The native integration tests in `tests/test_workload.py` run the same JSON
recipe on local machines and the Windows/Linux CI test jobs. They cover
readiness, exercise arguments, capped/redacted logs, closed stdin, failed
readiness with an unrelated surviving process, exercise-child cleanup, and
interruption. They use disposable synthetic workloads rather than a real
service or production credentials.
