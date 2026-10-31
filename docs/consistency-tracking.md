# Evidence and presentation consistency tracking

This reconciles the original I7 scope in
[issue #29](https://github.com/JunaidAhamed-7777/DrainTracer/issues/29).
Linked changes have their own implementation and regression evidence; this
umbrella does not reimplement them or treat unrelated fixes as substitutes.
The parent closes after its linked implementation scope is delivered.

- **Unjudged headless signal wording:**
  [#65](https://github.com/JunaidAhamed-7777/DrainTracer/issues/65),
  [PR #91](https://github.com/JunaidAhamed-7777/DrainTracer/pull/91).
  Collecting/unavailable states do not read as clear without a judgment.
- **Unknown counters in focused terminal views and HTML:**
  [#9 / PR #78](https://github.com/JunaidAhamed-7777/DrainTracer/pull/78) and
  [#14 / PR #83](https://github.com/JunaidAhamed-7777/DrainTracer/pull/83).
  Unreadable values remain unavailable rather than measured zeroes.
- **Per-resource coverage and HTML verdict qualification:**
  [#15 / PR #84](https://github.com/JunaidAhamed-7777/DrainTracer/pull/84),
  plus [#13 / PR #70](https://github.com/JunaidAhamed-7777/DrainTracer/pull/70)
  for exact partial-coverage preservation in comparisons.
- **Headless process counts:**
  [#66 / PR #92](https://github.com/JunaidAhamed-7777/DrainTracer/pull/92).
  Progress uses sampled live/start/exit/restart counts without extra resampling.
- **Analysis and diff process attribution:** implemented in this issue's
  focused changes, with bounded independent per-side lifetime and retained
  alert rankings. The [scope guide](report-process-attribution.md) documents
  caps, missing/duplicate identities, startup inclusion, and the distinction
  between alert capacity and lifetime changes. No cross-run identity pairing
  or per-process regression verdict is invented.
- **Attribution arithmetic decision:** retain the detector's positive-only
  contributor model. The evidence view now labels both amounts and percentage
  shares as positive growth, with an explicit offset explanation. Decreases
  and exits can reduce net window growth below those positive components.
  The mixed-growth/exit regression verifies the denominator without changing
  severity or detection math. This is part of #29; the separate
  [#10 / PR #79](https://github.com/JunaidAhamed-7777/DrainTracer/pull/79)
  fixes literal parent labels and is not credited as an arithmetic fix.
- **Fractional count budgets:**
  [#63](https://github.com/JunaidAhamed-7777/DrainTracer/issues/63),
  [PR #67](https://github.com/JunaidAhamed-7777/DrainTracer/pull/67) and
  [PR #68](https://github.com/JunaidAhamed-7777/DrainTracer/pull/68).
  Handle/thread limits require whole counts.
- **Help input isolation:**
  [#7 / PR #71](https://github.com/JunaidAhamed-7777/DrainTracer/pull/71).
  Destructive/global target controls do not act behind help.
- **Configured budget display:**
  [#64 / PR #102](https://github.com/JunaidAhamed-7777/DrainTracer/pull/102).
  Effective resource limits are visible in dashboard settings/help.
- **Budgets recorded in reports:** tracked by
  [#37](https://github.com/JunaidAhamed-7777/DrainTracer/issues/37), including
  explicit flag precedence and controlled baseline checks. This linked
  implementation must be delivered before the I7 parent closes; this
  reconciliation does not claim a pending change is already merged.

## Deliberate lifecycle-detail retention decision

Keep per-worker lifecycle text when at most three events of a kind arrive in
one sample; larger same-sample bursts are coalesced. There is no new temporal
rate limiter across samples. This preserves useful individual names/ordering
for recent investigation while accepting that steady one-worker-per-sample
churn can evict older text. Do not present the event log as a complete audit
of every process ever created.

Memory remains bounded: 200 text events, 256 recent lifecycle events, 500
timeline marks, bounded snapshot history, and bounded exited-process details.
[#56 / PR #101](https://github.com/JunaidAhamed-7777/DrainTracer/pull/101)
exports exact text-event observed/retained/dropped counts; these count log
entries, not process starts hidden inside a coalesced entry.
[#6 / PR #93](https://github.com/JunaidAhamed-7777/DrainTracer/pull/93)
preserves producer-owned resource facts before queue eviction; it is not a
cross-sample lifecycle rate limiter. Process lifecycle totals and retained
detail have separate scopes, and chart/queue/event omissions remain explicit.

This is an intentional documented limit under #29's completion criterion.
It avoids silently discarding repeated lifecycle events under a new throttling
rule. A future temporal aggregation policy needs its own definition of
detail loss, counters and flush/shutdown behavior; it must not reuse the
resource-sampling queue-drop count as an event-loss count.

No human acceptance, pilot, notification demand, operating objective, or
platform-demand criterion is resolved by this reconciliation. Issues
[#31](https://github.com/JunaidAhamed-7777/DrainTracer/issues/31),
[#49](https://github.com/JunaidAhamed-7777/DrainTracer/issues/49),
[#50](https://github.com/JunaidAhamed-7777/DrainTracer/issues/50),
[#51](https://github.com/JunaidAhamed-7777/DrainTracer/issues/51) and
[#53](https://github.com/JunaidAhamed-7777/DrainTracer/issues/53) retain their
separate evidence requirements.
