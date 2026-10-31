# Observer lifecycle and failure drills

All ten drills completed on native Windows and Linux under WSL, using source
`204c956c9452c8a523963fc7f72c1c1334310d7f` (candidate `v5.4.0`). Native
Windows ran Python 3.14.7 on build 26300; WSL ran Python 3.14.4 on kernel
`6.18.33.2-microsoft-standard-WSL2`. Both used psutil 7.2.2 and Rich 15.0.0
on the shared developer machine. These are bounded local reference exercises,
not a consent-based user pilot or production reliability guarantee.

The [Windows record](measurements/observer-stress-windows-2.json) and
[Linux record](measurements/observer-stress-linux-2.json) contain source hashes,
test selections, conditions, and identity-free outcomes. All target processes
were disposable, with independent lifetimes of at most 15 seconds. Native
sampling used 200 ms intervals and two-second windows. Successful owned
cleanup was checked against observed PID/creation-time identities without
persisting them in the published aggregates.

These records precede a later multi-process review finding: Windows raw
FILETIME and psutil floating-point conversion can differ by one representable
step for some process starts. A successful single fixture/control sequence
does not establish equality across starts or cover every access-denied
fallback. The identity implementation and additional native fallback evidence
require separate final acceptance; do not treat these historical records as
that guarantee.

## Reproduce

Install the project dependencies, use the recorded source in a clean checkout,
and run each environment's Python with a new output filename:

```powershell
python torture/observer_stress.py --output torture/reports/observer-stress-windows.json
```

```sh
python torture/observer_stress.py --output torture/reports/observer-stress-linux.json
```

The output is checkpointed between cases and refuses existing destinations.
Any failure stops the sequence and preserves its active case and error type.
Raw report pairs stay under the ignored neighboring report directory.

## Observed outcomes

- **PID reuse and denied identity queries:** ten deterministic identity/control
  regressions on Windows; nine executed and one Windows-only test skipped on
  Linux. Zero failures or errors. Replaced roots, repeated descendant opens,
  retired PIDs, tiny Windows creation-time changes, unknown original times,
  denied current queries, and pending resume debt all use conservative control
  rules. This does not claim to force real kernel PID reuse.
- **Short-lived parent:** a native parent survived 1.5 seconds after spawning
  its 12-second child. Two identities were discovered on each platform, with
  12 Windows and 13 Linux samples showing a surviving child after root exit.
  The four-second monitor limit ended and cleaned that owned child.
- **Access loss/recovery:** inject root-only memory `AccessDenied` from
  0.8–2.4 seconds while the real child and identity checks remain active.
  Each platform recorded eight partial samples, followed by 18 Windows /
  19 Linux full samples. The first restored sample had no stale judged
  window; fresh windows subsequently filled. Cleanup completed.
- **Collector stall:** inject one 1.2-second collector stall into real native
  sampling. Each platform recorded one sampling gap, no clock adjustment,
  and fresh unjudged windows at the reset. No HIGH failure was manufactured.
  This models an unobserved interval; physical sleep/wake was not tested.
- **Clock steps:** inject a +600-second sampler wall-clock offset, then remove
  it, leaving the real monotonic clock and host clock untouched. Each platform
  recorded two clock adjustments, zero sampling gaps, and restarted windows
  at both transitions. No HIGH failure was manufactured.
- **Slow consumer:** pause consumer polling for three seconds with a four-item
  queue while the native sampler continues. Both platforms retained four queue
  items, recorded ten drops, and had exact producer facts for 15 observations
  before polling resumed. Producer count and peak survived eviction; export
  remained readable. Queue drops remain explicit evidence loss for details.
- **Disk exhaustion:** inject `ENOSPC` at the atomic replacement boundary after
  writing the temporary file. Both runs returned monitoring/export error `2`,
  left zero temporary artifacts, retained in-memory evidence, and completed
  owned cleanup. The disk was not filled; filesystem, device, and network
  storage failure varieties beyond this boundary are not covered.
- **Ctrl+C after JSON:** interrupt HTML generation after JSON is saved. Both
  runs returned `130`, retained the same report ID in corrected JSON, recorded
  `HTML report interrupted`, and saved interruption status. No owned target
  survived. An existing leak failure still has precedence under normal policy.
- **Ctrl+C before JSON:** interrupt the initial JSON export. Both returned
  `130`, reported no saved report, retained in-memory evidence, left no
  temporary artifact, and completed owned cleanup.
- **Attached pause/ordinary shutdown:** the fixture owner launched an external
  bounded process, verified its creation time, then the monitor explicitly
  paused it. Ordinary monitor shutdown resumed the same identity and detached
  without terminating it. The fixture owner alone terminated it afterward.

The first complete attempt at each platform's sequence passed nine drills,
then failed before attachment monitoring because the harness passed an
unsupported `HeadlessRunner` constructor keyword. Preserve those
[Windows](measurements/observer-stress-windows-1.json) and
[Linux](measurements/observer-stress-linux-1.json) failed records. Commit
`204c956` uses the session's supported identity field and ensures fixture
cleanup also covers setup exceptions. Both entire ten-case sequences were
then rerun once; the records above are those fresh complete sets. No
production-code fix or selectively retried individual case is hidden by this
amendment. The original disposable attachment fixtures had 15-second
self-limits; the corrected harness also cleans through its owned Popen object
when attachment setup fails.

## Discovery and unexpected-crash policy

Process-tree polling observes snapshots of a changing system. A worker that
starts and exits between discoveries may be missed. A very short-lived parent
can hide a descendant before ownership is proven. Windows searches recently
departed identities for bounded orphan discovery; Linux launch sessions help
retain reparented descendants, but a worker that creates a different session
before discovery can escape that association. The measured parent-survival
interval is not a guarantee for arbitrarily short lifetimes or protected
process trees. Partial/unavailable counters and unverified identities remain
qualified, and unreadable creation time cannot authorize a process control.

Normal quit, timeout, and handled interruption stop sampling and attempt owned
launch cleanup; attachment shutdown resumes only pauses DrainTracer owns and
then detaches. A failed safe resume retains its debt for a verified retry.
A process that was already stopped before attachment must remain stopped.
Read [process identities](process-identities.md) for exact matching and denied
query behavior. These rules avoid treating an old PID as lasting authority;
validation and an OS operation still have a finite race interval.

An abrupt monitor termination, `os._exit`, OS crash, or power loss can prevent
Python cleanup/finally blocks and the final export from running. DrainTracer
does not promise automatic target termination or resume after such a crash,
and it does not install a recovery watchdog or kill-on-close job policy.
A launched target may outlive the monitor, and an attached target paused by
the monitor may remain stopped. A saved report is the last persisted evidence,
not a live ownership token: independently verify the current process identity,
permissions, and intended state before using OS tools or explicitly reattaching.
Never blindly resume or kill every PID from an old report. This documents the
failure boundary; it is not a claim that abrupt termination runs normal cleanup.

The separate [observer measurement protocol](observer-protocol.md) covers
reference-derived budgets, full-retention export stress, and the 30-minute
native soak. These short fault drills alone do not establish observer budgets.
