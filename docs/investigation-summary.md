# Shareable investigation summaries

Generate a copyable account of one resource's evidence from a saved report:

```powershell
draintracer-report investigate .\run.json --runtime python --format text
draintracer-report investigate .\run.json --resource handles --profile minimal
```

The default output is JSON with `investigation_schema: 1` and a `text`
field. `--format text` prints that text directly for copying into a bug
report. Select `--resource memory|handles|threads`, or let the command choose
the resource with the strongest recorded signal. Select
`--runtime unknown|python|dotnet|native` explicitly; the command does not
guess a runtime from a confidential process name.

The privacy transformation runs **before** summary composition. Both
`share` and `minimal` remove source paths, names, identifiers, dates, labels,
and free-text messages. The summary retains the metric, strongest severity,
reported growth window, numeric change/slope, leading pseudonymous process
where attribution survives, coverage counts, and peak provenance. Missing
attribution, unknown coverage, and details lost to thinning remain explicit.
The output describes heuristic resource growth and cannot establish an
allocation source or source-code root cause.

Profiler guidance depends on the selected metric, explicitly selected
runtime, and recorded platform. Windows summaries suggest matching
[WPR/WPA profiles for resource and process/thread lifetimes](https://learn.microsoft.com/en-us/troubleshoot/windows-server/support-tools/support-tools-xperf-wpa-wpr).
Python memory summaries suggest comparing
[tracemalloc snapshots](https://docs.python.org/3/library/tracemalloc.html),
which trace Python allocation blocks rather than all process-private memory.
.NET memory summaries describe
[dotnet-gcdump](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/dotnet-gcdump)
for managed objects and state that collection triggers garbage collection.
These are suggestions for a separate bounded investigation: the command
does not run a profiler, control the target, or upload evidence.

Process IDs in shareable text are pseudonyms. Consult the private original
to locate the actual process before profiling. Review copied text before
sharing because resource/timing patterns remain visible. See the
[privacy guide](privacy.md) for the complete transformation and its limits.
