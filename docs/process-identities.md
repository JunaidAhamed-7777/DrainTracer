# Process identity checks

A PID identifies a slot, not a permanent process. DrainTracer records the
creation timestamp with each tracked PID. On Windows, every representably
different creation timestamp is a different identity; a five-millisecond
change is not accepted as clock tolerance. The Linux policy remains separate
because its creation times derive from boot time and process start ticks.

The minimum supported psutil 5.9 Windows implementation obtains creation time
through [GetProcessTimes](https://github.com/giampaolo/psutil/blob/release-5.9.0/psutil/_psutil_windows.c#L215).
Convert the integer FILETIME epoch offset before dividing into floating-point
seconds; dividing before subtracting can produce a different rounded float.
Comparisons here use psutil's own creation-time representation consistently.

On the local Windows reference host with Python 3.14.7 and psutil 7.2.2,
1,000 fresh Process creation-time reads and 100 real fallback reads (with the
initial lookup deliberately denied) each yielded one unique float with zero
spread. Both matched the creation FILETIME obtained through the owned Popen
handle with integer-epoch subtraction before division. The regression repeats
200 ordinary and 20 fallback reads against a fresh owned child on Windows,
including the supported dependencies in CI. These observations do not claim
exhaustive PID reuse or scheduler coverage.

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
