# Process identity checks

A PID identifies a slot, not a permanent process. DrainTracer records the
creation timestamp with each tracked PID. On Windows, every representably
different creation timestamp is a different identity; a five-millisecond
change is not accepted as clock tolerance. The Linux policy remains separate
because its creation times derive from boot time and process start ticks.

The minimum supported psutil 5.9 Windows implementation obtains creation time
through [GetProcessTimes](https://github.com/giampaolo/psutil/blob/release-5.9.0/psutil/_psutil_windows.c#L215).
Its [shared conversion](https://github.com/giampaolo/psutil/blob/release-5.9.0/psutil/_psutil_common.c#L348-L370)
subtracts the integer FILETIME epoch offset, casts that difference to a C
double, then divides by 10,000,000. The equivalent Python expression is
`float(ticks - 116444736000000000) / 10000000.0`. Python integer division
without that explicit cast can round differently. Comparisons here use
psutil's published float consistently; this representation does not retain
every 100-nanosecond FILETIME tick.

On the local Windows reference host with Python 3.14.7 and psutil 7.2.2,
an initial one-process audit had zero spread across repeated fast and fallback
reads. A subsequent audit of 40 newly started children found nine cases where
Python integer division differed from psutil by one float step; all 40 fast
and fallback results agreed exactly and matched the explicit C-style cast.
The regression now starts 40 separate owned children, reads each creation
FILETIME through its original Popen handle, and checks five fresh fast reads
and three real fallback reads (with the initial lookup deliberately denied)
against that conversion and each other. CI also runs the minimum supported
psutil 5.9.0 on Windows. These observations do not claim exhaustive PID reuse,
raw FILETIME identity, or scheduler coverage.

Another regression supplies a five-millisecond difference and the next
representable timestamp. It checks that the old identity is preserved and no
process-control call reaches the replacement. It simulates reuse rather than
claiming to force the OS to recycle a particular PID on demand. Unreadable
identities and the interval between validation and an OS operation require
their own conservative handling; this fix removes incorrect ten-millisecond
identity equivalence without claiming every control race is eliminated.

Process-control opens separately require a recorded original creation time
and a currently readable matching time. A denied current query still permits
read-only observation, but cannot authorize pause, resume or termination.
Initially unknown tracked identities are not promoted by later observations;
the original root also needs a known identity before controlling its tree.
Reattach explicitly once the required identity can be verified. If an owned
pause cannot safely be resumed, its resume debt remains visible for retry;
a proven exit or PID replacement clears that debt without touching a bystander.
