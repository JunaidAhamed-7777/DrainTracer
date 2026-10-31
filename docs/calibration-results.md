# Repeated calibration results

The default detector detected all three fast-growth controls on both tested
platforms, but it also warned about bounded cache/startup growth. The slower
0.5 MiB/s memory fixture produced LOW warnings and never HIGH during 75 seconds.
An exit code of zero under the default HIGH policy therefore does not mean
there was no warning or no known growth. **No default threshold is changed.**

## Reproduce and inspect the evidence

The [protocol](calibration-protocol.md) was committed before measurement.
Use its commands and source commit
`6d398f7c1e79e6a3608e03d0a7b31e5faccaf226` to reproduce the recorded inputs.
The [Windows aggregate](measurements/calibration-windows.json) and
[Linux aggregate](measurements/calibration-linux.json) preserve every run,
including all warning tiers, first-warning times, individual window judgment
counts, coverage, settings, source hashes, and observer diagnostics. No run
was discarded or selectively rerun. The source reported candidate `v5.4.0`;
these measurements do not represent a later release merely because the
documentation is included in it.

Each platform completed 12 scenarios, repeated in the same order three times:
36 runs of 75 seconds, 500 ms sampling, default 10/30/60-second windows and
default thresholds. Warm-up was three seconds except for the cache and
unqualified startup cases. Targets had independent 90-second lifetimes.
First-warning times below are seconds from monitor launch, including startup
and warm-up; they are not independently measured allocation-to-alert latency.
Warning tiers describe the **worst historical warning**, including a ramp
that later plateaued, rather than only the final snapshot.

## Machine conditions

Both sets ran concurrently from approximately 12:29 to 13:14 UTC on
2026-10-31, sharing the developer machine with other work. This is not an
isolated throughput benchmark.

- Native Windows 11 build 26300, AMD64, Python 3.14.7, psutil 7.2.2,
  Rich 15.0.0, 16 logical CPUs, 16,477,028,352 bytes physical memory.
  Available memory at the start was 1,570,529,280 bytes and host CPU 47.2%.
  The per-run record also captures pressure immediately before each case.
- Linux under WSL2 kernel `6.18.33.2-microsoft-standard-WSL2`, x86_64,
  Python 3.14.4, the same dependency versions and 16 logical CPUs,
  7,977,201,664 bytes guest physical memory. Initial guest available memory
  was 7,190,278,144 bytes and CPU 7.1%. Guest availability does not describe
  independent physical host capacity.

Low Windows available memory and the shared host limit what can be inferred
about speed or general accuracy. Source file hashes and all per-run host
conditions remain in the aggregates so those conditions are reviewable.

## Growth and warning results

Every count below has three repetitions per platform. Windows and Linux are
reported separately where their timing or warning resources differ.

- **Stable:** 8 MiB held, four open files, two waiting threads. No warning
  in 3/3 Windows or 3/3 Linux runs.
- **Cache then plateau:** 1 MiB per 250 ms to 24 MiB, zero warm-up.
  Memory LOW in 3/3 on each platform: Windows first warning 8.54–8.57 s;
  Linux 8.50 s. No HIGH. This is a known benign-retention warning.
- **Startup spike:** 96 MiB over 2.5 s then held, zero warm-up.
  Memory CRITICAL in 3/3 on each: Windows 8.57–8.66 s; Linux 8.50–8.51 s.
  This is a known benign-startup warning, not proof of an allocation leak.
- **Same startup, three-second warm-up:** no warning in 3/3 on either
  platform. The warm-up covers this fixture's startup; it does not establish
  a suitable duration for arbitrary applications.
- **Allocation/recovery:** repeated touched 32 MiB allocation, one-second
  hold, release, one-second wait. No warning in 3/3 on each platform.
- **Worker churn:** a child every 500 ms, 750 ms child lifetime, at most
  four active children. No warning in 3/3 on each. Observed maxima were
  three simultaneous processes on Windows and two on Linux. This is sampled
  discovery, not proof that every short-lived child was observed.
- **Fast memory growth:** 2 MiB per 250 ms, capped at 128 MiB. Memory HIGH
  in 3/3 on each: Windows 11.57–12.05 s; Linux 12.00 s. The cap makes the
  later part a plateau; the warning is from the growing prefix.
- **Slow memory growth:** 0.25 MiB per 500 ms, 48 MiB cap, still growing
  through the measured interval. Memory LOW in 3/3 on each: Windows
  54.07–54.58 s; Linux 54.00–54.50 s. HIGH occurred in **0/3 on each**.
  These six missed HIGH-policy detections remain explicit, even though LOW
  correctly recorded growth and default headless exit status was zero.
- **File-handle growth:** ten files per 250 ms, cap 800. Intended-resource
  HIGH in 3/3 on each: Windows 11.55–12.04 s; Linux 11.50–12.00 s.
  Linux measures file descriptors; it is not the Windows handle namespace.
- **Thread growth:** one blocked worker per 250 ms, cap 80. Thread HIGH in
  3/3 on each: Windows 11.55–12.06 s; Linux 11.50–12.00 s. Windows also
  recorded handle LOW in 3/3, first 28.55–29.06 s; Linux did not. The
  additional Windows signal is preserved rather than hidden as irrelevant.
- **Partial access:** a stable root plus one child, 1 MiB each. Only the
  sampler's root memory read was forced to raise `AccessDenied`. All samples
  were qualified partial, with one unavailable initial sample per run;
  subsequent child evidence could still fill windows. There were no warnings.
  Root sampling fails as a unit, so the aggregate marks handles and threads
  partial as well as private memory. This is not full-root clean evidence.
  Injection affected observation only; real identity and cleanup checks
  remained enabled. It does not reproduce all real ACL transitions.
- **Larger stable tree:** depth three, fan-out two, 1 MiB per logical
  process. All 15 logical processes were observed in every run; no warning
  in 3/3 on either platform.

Across the seven benign full-coverage scenarios (stable, cache, startup,
startup with warm-up, recovery, churn, larger tree), **6/21 runs per platform
had any warning and 3/21 had HIGH or stronger**. Cache and startup account
for those warnings. These are fixture-run counts, not a false-positive
probability. The partial-access case is excluded from this denominator.
Across four intended-growth scenarios, the intended resource warned in
12/12 runs per platform and reached HIGH in 9/12; the three slow-memory runs
per platform account for the HIGH misses.

## Coverage, durations, and profiles

Windows recorded 5,413 samples and Linux 5,434. Each platform had 33
full-coverage runs and three deliberately partial runs. All runs produced
JSON/HTML pairs, judged detection windows, and completed owned cleanup.
There were zero failed samples, queue drops, monitor errors, export failures,
or operational failures. "Judged" alone does not remove partial coverage.

In full-coverage runs, the first judged 10-second window appeared at
8.54–12.08 s on Windows and 8.50–12.00 s on Linux; the 30-second window at
25.55–29.08 s and 25.50–29.01 s; the 60-second window at 51.04–54.59 s and
51.00–54.51 s. These ranges combine zero/three-second warm-up cases. The
detector's 85% window-coverage rule explains why a named window can first
judge before its full nominal duration. Scheduling and discovery still add
time, and a shorter session can remain unjudged.

For these fixtures and the tested 500 ms cadence:

- Use **at least 15 seconds** after launch for a smoke observation of fast
  memory, handle, or thread growth with three-second warm-up. This is a
  conservative practical floor from these measured delays, not an exact
  minimum established by a separate duration sweep or enough evidence for
  the 30/60-second windows.
- Use the tested **75-second duration** when the intention is to exercise
  all default windows after three-second warm-up, or to observe this slow
  memory fixture. The slow fixture first warned around 54–55 seconds;
  a 15-second smoke would miss that signal.
- Select warm-up from the workload's actual startup phase and retain
  startup peaks separately. Three seconds removed the measured startup
  warning here; cache filling and slow startup may take longer. Blindly
  increasing warm-up can hide relevant early growth.
- For CI that must react to this slow growth, the tested default HIGH
  policy is insufficient. A workload-specific `fail_on = "low"` policy
  can act on its LOW warnings, but would also fail the measured benign cache
  ramp without a suitable workload/warm-up design. Keep matched benign and
  growth controls before adopting that policy or changing thresholds.
- Recalibrate after changing cadence, windows, thresholds, warm-up, input
  size, process topology, Python/platform, or permissions. The example
  `heavy` profile is illustrative configuration; it has not been measured
  by this dataset and must not inherit these timing claims.

See [configuration](configuration.md) for `--interval`, repeatable `--window`,
`--warmup`, `--timeout`, and `--fail-on`. Retain
`--fail-on-inconclusive` when an unjudged headless run should fail CI, while
checking partial coverage independently.

## Boundaries

There are three repeats, bounded synthetic fixtures, at most 15 logical
processes, and one shared machine. No production workload, human pilot,
cause attribution, native Linux host, physical sleep/wake, or universal
accuracy claim is supported. Allocation caps limit fixture growth, and RSS
retention can differ from live allocation. Process polling can miss a child
that starts and exits between discoveries. Headless observations do not
benchmark interactive terminal rendering.

The measured source predates the later temporal-reset hardening. Its largest
recorded inter-sample gap was 657.0 ms on Windows and 506.8 ms on Linux,
well below the later default two-second continuity-reset boundary; there is
no reason to erase these historical measurements. Recollect to assess a
different source or environment rather than relabeling the recorded commit.
Observer budgets and lifecycle stress/soak testing are separate work under
issue #26; these short calibration runs alone do not establish those budgets.
