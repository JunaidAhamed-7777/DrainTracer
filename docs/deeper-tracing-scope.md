# Deeper tracing scope

DrainTracer retains process-tree counters and existing PID/creation-time
history, contributor ranking, lifecycle events, and new/existing growth
attribution. It does not install an allocation tracer. Those counters can
identify growing resources and contributing processes; they cannot locate an
allocation site or prove why memory is retained.

Issue #52 evaluates tracing only where the investigation needs justify it.
The bounded worker fixtures distinguish accumulation from growth in an
existing process using delivered attribution. They do not demonstrate a
production requirement for stacks, heap objects, native injection, or elevated
system tracing. The consent-based human requirements work remains open in
issue #49. Choosing no adapter here is a scope decision, not a claim that
deeper investigation is unnecessary for every application.

## Candidate boundaries

**Cooperative Python tracing:** `tracemalloc` records Python allocation
tracebacks and statistics. It must start in the target runtime; earlier
allocations are absent, and native extension coverage depends on the memory
domains they trace. Extra frames increase CPU/memory cost, and its own trace
storage can be measured. Tracebacks include filenames and line information.
[Python's tracemalloc documentation](https://docs.python.org/3.14/library/tracemalloc.html)
describes these limits. An integration would therefore need target-side
cooperation and a separate output protocol; that is an architectural
assessment, not a remote-attach capability supplied here. A target able to
run the module does not justify requesting broader system privileges.

**Windows native heap tracing:** WPR's documented heap workflow includes
per-executable IFEO configuration, an elevated configuration step, capture,
stop, and disabling the configuration afterward.
[Microsoft's heap recording guide](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/recording-for-heap-analysis)
defines that lifecycle. Stack-based heap analysis needs symbol decoding;
capture can begin at launch or attach later, while later capture omits
earlier allocation history. Trace buffers consume finite non-paged memory,
and early heap activity can create large traces.
[Microsoft's heap capture guide](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/enabling-heap-data-capture)
describes those boundaries. This is a materially different ownership and
privilege surface from ordinary counter sampling. No registry change,
system trace session, injection, driver, or native adapter is installed by
this work. Windows documentation does not establish equivalent Linux support.

Neither candidate has a measured adapter cost in DrainTracer because neither
is implemented or run as an adapter. The observer budgets from issue #26
apply to the recorded aggregate sampler and export, not an unmeasured tracing
configuration. A generic statement that a tracing technology is efficient
cannot substitute for a workload-specific measurement.

## Acceptance boundary for a future opt-in adapter

Any future adapter proposal must start with an actual investigation question
that counters and existing attribution cannot answer. It must define which
runtime/platform and allocation domains it covers, required privileges and
target cooperation, startup/attach behavior, missed history, and failures.
The proposal needs matched tracing-off/tracing-on measurements for target
throughput/latency, observer/target CPU and memory, trace volume, loss, and
export time, with bounded duration/storage and cleanup of configuration and
capture sessions.

Tracing must remain explicit opt-in. Paths, source locations, stacks, symbols,
and any provider payload need a separate privacy review and typed export
allowlist before sharing. The assessment here is that these fields can reveal
application structure or local paths; existing report redaction must not be
assumed to understand arbitrary trace files. Keep local retention, access,
deletion, and upload choices explicit and document unsupported/protected
targets. Do not silently enable a tracer, upload captures, or interpret
absence of trace evidence as a clean allocation result.

This boundary permits a later justified, measured adapter without claiming an
adapter or human-pilot result has been delivered now.
