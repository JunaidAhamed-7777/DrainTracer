# Preflight and project initialization

These commands inspect configuration and paths without launching, pausing,
resuming, or terminating a target. Monitoring still uses the ordinary startup
and cleanup rules described in [Usage](usage.md).

## Command contract

`draintracer doctor` accepts the normal configuration, profile, sampling,
target, PID, timeout, and budget flags. It resolves the same settings as a
monitoring run and shows each value's origin: default, config file, selected
profile, or command line. It checks the selected target/interpreter, current
PID readability when requested, report-directory access, and whether the
duration leaves enough evidence after warm-up.

Intervals are milliseconds; windows, warm-up, and timeouts are seconds.
Private-memory thresholds use bytes and bytes/second; handle/thread thresholds
use counts and counts/second. Budget origins and threshold origins are tracked
per resource, so an inherited value is not attributed to a later partial override.

The output-directory check creates and removes one temporary probe in the
directory, or its nearest existing parent when the directory does not yet
exist. It does not create the report directory. Readability checks describe
the current state; permissions and processes can change before monitoring.
Target arguments and environment variables are not printed or executed.

`doctor --json` emits diagnostic schema 1 with resolved settings, origins,
and checks. Exit `0` means there are no blocking checks; warnings remain
visible. Invalid settings, unavailable target paths, and failed access probes
exit `2`. `--plain` and `--no-color` apply to the text presentation.
Command syntax errors use the normal argparse diagnostic on stderr and exit
`2`; they do not produce the JSON result. Diagnostics include local paths,
the profile name, numeric limits, and configuration origins. Review those
before sharing the output. Free-text experiment labels, target arguments,
and environment values are omitted.

`draintracer init` previews a minimal `.drainrc.toml`. `--path PATH` chooses
the destination, and `--write` explicitly creates it. The generated TOML is
validated through the ordinary configuration loader before creation.
Creation is exclusive: existing files are never overwritten, even if they
already contain the generated text. The parent directory must exist.
Preview also checks that parent. A failed write can leave a new partial file;
the error identifies this state and asks you to inspect it before retrying.

`init --json` emits initialization schema 1 with the destination, preview,
and whether a file was written. Preview does not change project files.
Invalid destinations, existing files in write mode, and write failures exit
`2`. Initialization never starts a target or changes system settings.

## Examples

```powershell
draintracer doctor --target .\torture\steady.py --timeout 30s --warmup 2s
draintracer doctor --config .drainrc.toml --profile ci --json
draintracer init
draintracer init --path .drainrc.toml --write
```

The fixture example needs a source checkout. For an installed application,
substitute the executable or script you intend to monitor.
