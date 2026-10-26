# Configuration

[README](../README.md) · [Usage](usage.md) · [Reports](reports.md) · [Example settings](../examples/drainrc.toml)

Settings are TOML. Copy the commented example into the project whose
workload you want to monitor:

```powershell
Copy-Item .\examples\drainrc.toml .\.drainrc
python src/main.py --profile ci --headless --target .\torture\steady.py
```

The example defines `ci`, `heavy`, and `strict` profiles. Choose settings
for a representative workload; increasing thresholds can hide real growth
as well as reduce unwanted signals.

## Discovery and precedence

DrainTracer searches the current working directory, then each parent.
Within a directory it checks `.drainrc`, `.drainrc.toml`, then
`draintracer.toml`. A `config.toml` is considered only when it contains a
`[draintracer]` table. The nearest matching file wins; files are not merged
across directories. Discovery is based on where DrainTracer is run, not on
the target's directory.

Use `--config FILE` to choose explicitly or `--no-config` to ignore files:

```powershell
python src/main.py --config .\examples\drainrc.toml --profile strict --target .\torture\memory_leak.py
python src/main.py --no-config --interval 250ms --window 10s --window 30s --target .\torture\steady.py
```

Later layers override earlier ones:

1. Built-in defaults.
2. Base settings from the selected file.
3. The selected profile, from `--profile` or the file's `default_profile`.
4. Command-line flags.

Profiles override individual fields. A partial threshold override preserves
the remaining resource thresholds. Repeated `--window` flags replace the
entire configured window list; values are deduplicated and sorted.
`--profile` requires a file and cannot accompany `--no-config`.
Unknown settings, unknown profiles, invalid units, and inconsistent durations
are rejected before a process is launched or attached.

## Settings reference

- `sample_interval`: time between samples; default `"500ms"`. Allowed range
  is `"100ms"` through `"1m"`. CLI: `--interval DURATION`.
- `windows`: one to eight distinct positive durations; default
  `["10s", "30s", "60s"]`. Each is at most `"1h"` and must be at least
  four sample intervals long. CLI: repeat `--window DURATION`.
- `warmup`: active sampling time before leak detection starts; default
  `0`, maximum `"24h"`. CLI: `--warmup DURATION`.
- `timeout`: positive session time limit; absent by default. If warm-up is
  enabled, it must be longer than the warm-up. CLI: `--timeout DURATION`.
- `fail_on`: headless failure threshold, `"low"`, `"medium"`, `"high"`,
  or `"critical"`; default `"high"`. CLI: `--fail-on SEVERITY`, which
  requires `--headless`.
- `fail_on_inconclusive`: `true` makes a headless run that judged no
  detection window exit `3` instead of `0`; default `false`. CLI:
  `--fail-on-inconclusive`.
- `fail_on_target_error`: `true` makes a headless run exit `4` when a
  launched target exits by itself with a non-zero code; default `false`.
  CLI: `--fail-on-target-error`.
- `workload_id`, `build`, `note`: non-empty strings recorded in the report's
  `experiment` section for comparisons; secret-looking values are redacted.
  CLI: `--workload-id ID`, `--build LABEL`, `--note TEXT`.
- `operations`: positive operation count or input size of the workload;
  comparisons show growth per operation when both runs set it. CLI:
  `--operations N`.
- `budget_private`, `budget_handles`, `budget_threads`: absolute peak
  ceilings, a size such as `"500MB"` for private bytes and a whole-number
  count such as `2000` for handles and threads; `1.5` is rejected, while a
  whole-valued float such as `2000.0` is accepted. A headless run whose
  whole-run peak exceeds one exits `5`. CLI: `--budget-private SIZE`,
  `--budget-handles N`, `--budget-threads N`, which require `--headless`.
- `report_dir`: output root; default `"leak_reports/reports"`. Each export
  goes into a program-filename/date folder beneath it. A file's
  relative value is relative to that file. A relative CLI override is
  relative to the current working directory. CLI: `--report-dir DIR`.
- `default_profile`: name of an existing profile to apply when `--profile`
  is omitted. No profile is selected by default.
- `thresholds`: resource tables described below.

File settings can appear at the top level or inside `[draintracer]`. In a
shared file, profiles and thresholds must also be nested below that table,
for example `[draintracer.profiles.ci]`.

## Thresholds and units

```toml
sample_interval = "500ms"
windows = ["10s", "30s", "60s"]
warmup = "15s"
report_dir = "leak_reports/reports"

[thresholds.memory]
critical_delta = "50MB"
critical_slope = "5MB/s"

[thresholds.handles]
critical_delta = 500
critical_slope = "20/s"

[thresholds.threads]
critical_delta = 50
critical_slope = "2/s"

[profiles.ci]
timeout = "2m"
fail_on = "medium"

[profiles.heavy]
windows = ["30s", "60s", "2m"]
warmup = "30s"

[profiles.heavy.thresholds.memory]
critical_delta = "4GB"
critical_slope = "1GB/s"
```

The threshold values above are the built-in defaults. They normalize the
severity score; they are not hard cutoffs. A sufficiently persistent
smaller increase can produce a low signal below a critical reference.
Changing them preserves the positive-slope, monotonicity, recovery, and
window-coverage checks described in
[working-principle.md](../working-principle.md).

Durations accept numeric seconds or strings such as `"250ms"`, `"90s"`,
`"5m"`, and `"1h30m"`. Memory amounts accept bytes or strings such as
`"512KB"`, `"50MB"`, and `"1.5GiB"`. Units are binary throughout
DrainTracer: `MB` and `MiB` both mean 1,048,576 bytes. Counts accept numbers
or numeric strings. Rates are per second when numeric; strings can use
`/s`, `/min`, or `/h`, for example `"300MB/min"`. Both critical threshold
values must be greater than zero.

Sampling and threshold settings are recorded in reports. Keep those
settings, warm-up, workload, and duration comparable when using
[`diff`](reports.md#compare-two-runs).

Python 3.11+ provides TOML parsing in the standard library. On Python 3.10,
`requirements.txt` installs the `tomli` backport automatically.
