# Process evidence in report comparisons

`draintracer diff before.json after.json` displays the leading retained process
for each available resource and scope on each side. `--plain` preserves the
same words, `--quiet` omits detail, and `--json` includes up to five positive
contributors per ranking under each side's `process_attribution` field.
Resource verdicts and CI exit policies are unchanged by these rankings.

**A and B are ranked independently.** Equal PIDs, creation timestamps, names,
or positions in the two reports do not establish that they are the same
process. There is no cross-run process pairing or per-process regression
verdict. Use workload/source/settings comparability and aggregate evidence
before assessing whether a change helped.

## Two distinct scopes

**Lifetime first-to-last:** the report's existing bounded live/exited
`ProcessSummary` records supply the first and last measured value for each
process. Analysis reuses `ProcessSummary.growth` and ranks positive changes;
it does not trust a contradictory stored `growth` extension. Startup is
included. A worker first observed after allocating its memory can have zero
lifetime change even when adding that worker increased aggregate capacity.
Only retained lifetime summaries are represented; exited history can have
been evicted, readings can be missing, and this is not a sum over every
process ever created.

**Strongest retained alert:** select the resource's most severe retained
non-paused/non-failed alert, choosing the latest if equally severe. Show its
recorded interval, contributor deltas, and new-worker/existing-worker flags.
These values come from the detector's attribution; analysis does not run a
new detector or recompute allocation math. A stronger signal preserved in
whole-session facts can outlive all retained alert details, so unavailable
contributors never erase that signal. The lifetime and alert rankings can
identify different processes because their intervals differ.

The resource units are private bytes for memory, handle counts on Windows /
descriptor counts on Linux, and thread counts. A growing counter or leading
process is a direction for investigation, not an allocation site or proof of
source-code cause. Inconclusive, partial, and incompatible comparison caveats
still apply even when an informative process ranking is available.

## Positive attribution and net change

The evidence view labels contributor percentages as **shares of positive
growth**, and the new/existing amounts as positive components. Exits and
decreases can offset those components in the aggregate window change.
For example, an existing worker adding 30 MiB plus a new worker adding
20 MiB gives positive shares of 60% and 40%. Another worker releasing or
exiting with 40 MiB makes the net window increase only 10 MiB. The positive
amounts do not add to that net increase, and a contributor can legitimately
account for more than the net change. The detector's existing positive-growth
model and severity are retained; the display makes their denominator explicit.

## Missing identities and bounds

Unknown creation times remain unknown. Repeated copies of the same retained
PID/creation-time identity are omitted instead of merged or counted twice;
equal PIDs with different known creation times remain separate within a run.
JSON reports unknown-identity counts, invalid rows, duplicate omissions,
scan omissions and ranking limits. Terminal output names those qualifications.
Missing attribution is **unavailable**, not zero growth or a clean result.

The lifetime scan examines at most 4,096 combined live/exited summaries. Alert
selection examines the first 7,200 retained snapshots, at most eight alerts
per snapshot, and at most 256 contributors in the selected alert. Each ranking
returns the five largest positive rows by default; the analysis helper accepts
one through 20. Every applicable cap and omission count is explicit in JSON.
Older reports or minimal privacy profiles may lack these optional records.
No additional process is opened or controlled during offline analysis.

Process names are literal terminal text; markup-like names do not create styles
or links. Non-printing characters are replaced for display, long names are
bounded, and legacy code pages use the existing encoding-safe presentation.
Private report names/identities remain private data: comparison does not
implicitly anonymize its inputs. Apply and inspect the selected privacy
transformation before sharing evidence, and expect fewer identity details
after redaction.
