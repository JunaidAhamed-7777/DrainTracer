# A maintained Windows workload check

The [composite action](../.github/actions/draintracer/action.yml) installs an
exact DrainTracer version, validates a bounded recipe, collects evidence, runs
an optional strict report comparison, and uploads its evidence **before**
enforcing the recorded failure. It never approves or replaces a baseline.
The [native PR example](../.github/workflows/workload-example.yml) exercises
growing, changed and deliberately inconclusive workloads using this candidate's
wheel by default. Its optional published-package dispatch and released
consumers install the exact package version from PyPI.

## Copy the workflow

Prepare a [recipe and TOML profile](workloads.md) for your own disposable target.
Enable all three outcome policies: `fail_on_inconclusive = true`,
`fail_on_target_error = true`, and `require_full_coverage = true`. Recipes may
run for at most 300 seconds through this action. Keep equivalent interpreter,
target and checkout locations when comparing saved reports; changed recorded
execution choices correctly block strict equivalence.

```yaml
name: Resource regression
on: [pull_request]
permissions:
  contents: read
jobs:
  resources:
    runs-on: windows-2025
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
        with:
          persist-credentials: false
      - uses: JunaidAhamed-7777/DrainTracer/.github/actions/draintracer@v5.5.0
        with:
          version: '5.5.0'
          recipe: perf/workload.json
          # Optional: an owner-reviewed baseline from a protected source.
          # baseline: perf/approved-baseline.json
          artifact-name: resource-evidence-${{ github.run_id }}
```

Pin the action to the reviewed release's full commit SHA for immutable action
code; keep `version` an exact package version as shown. The separate `wheel`
input exists for testing a locally built candidate with matching metadata; it
is not a remote download URL. Setup uses Python 3.10 and the action creates a
runner-local environment tied to the pinned package or candidate-wheel digest.
Repeated calls use the same interpreter path. It does not install into or
modify the workload's own interpreter environment.

## Verify a published package

After the exact version is actually available on PyPI, dispatch
`workload-example.yml` at its matching release tag with `published_version`.
For example, **after** version 5.5.0 has been published and the tag contains
this workflow mode:

```powershell
gh workflow run workload-example.yml --repo JunaidAhamed-7777/DrainTracer --ref v5.5.0 -f published_version=5.5.0
```

Repeat with the matching tag/version for each later release. The tag selects
the reviewed action, fixture and verifier source; `published_version` selects
the package installed from PyPI. These are both required parts of the proof.
This example is a post-publication procedure, not a claim that the package or
its verification run already exists.

The input is passed through an environment variable and validated as an exact
ASCII `MAJOR.MINOR.PATCH` before writing action outputs. A leading `v`, leading
zeroes, prerelease suffix, whitespace or newline is refused. Nonempty input
skips the candidate build and explicitly leaves the `wheel` output empty, so
all three action calls take the isolated, exact-version PyPI install path.
An ordinary pull request or empty dispatch input still builds the checkout's
candidate wheel and uses its canonical version. Both modes copy the same
bounded disposable fixture.

The existing policy probe checks installed distribution metadata against the
requested version before running a recipe. The final verifier additionally
requires that version in **before, after and short-run JSON reports**. Strict
comparability, resolved memory evidence, growing/steady/short policy exits,
matching JSON/HTML, machine summaries and complete owned cleanup remain
required. Expected failing action steps still upload their evidence before
the verifier evaluates their results. Inspect all three retained artifacts;
a published-package check is incomplete if installation, comparison, version
or artifact validation fails.

The dispatched workflow retains `contents: read`, a 15-minute limit and
three-day artifact expiry. It needs no publishing secret, baseline-write
credential or additional workflow permission. It publishes no package or
baseline. Candidate-wheel success does not replace this check: keep the
published-installation gate open until the tagged published-package run
passes and its evidence has been inspected.

Use clean recipes without selected variables for strict comparisons. Selected
or inherited private environment values remain unrecorded and block strict
equivalence. Clean recipe comparisons still state that omitted Windows platform
locations and full OS state are outside their recorded-choice equivalence.
See [comparison policies](reports.md) and [controlled approvals](controlled-baselines.md)
for evidence gates. A completed diff can still report improvement in memory
alongside noisy or regressed resources; inspect each resource and qualification.

## Permissions, privacy and duration

Run untrusted pull requests with `contents: read`, checkout credentials disabled,
no production secrets, and no baseline-publishing token. Never use
`pull_request_target` to check out and execute untrusted PR code with elevated
credentials. The action requires no issue, pull-request, package or contents
write permission. Its annotations and job summaries use the runner's existing
output contracts rather than parsing Rich text.

An approved baseline must come from a protected commit or controlled W4 store.
Do not silently promote a pull-request artifact into one. Promotion belongs in
a separate trusted workflow, with owner-reviewed evidence and its own protected
environment/credentials. Keep publishing jobs away from PR checkouts and treat
all report claims as untrusted input. GitHub's [secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use)
describes the relevant read-only and script-input boundaries.

Each invocation retains matching JSON/HTML evidence when available, machine
summary, comparison JSON, exit/outcome record, and capped command logs. Files
are uploaded even after leak, workload, comparison or inconclusive failures.
Installation/startup failures can have diagnostics without a report; the action
returns an error rather than inventing evidence. Default retention is three
days. Logs cap at 2 MiB per command while remaining output is drained. Target
logs are captured only if the recipe opts in, with its separate stream limits.

Reports can disclose target paths, process metadata, timing and labels. Artifact
access follows repository visibility: public-repository artifacts are not private
storage. Review [privacy guidance](privacy.md) and restrict the repository or
transform evidence before any separate sharing. No secret is needed by the
synthetic example. This action uploads the explicitly requested CI evidence;
ordinary local monitoring does not upload anything.

The example samples for 16 + 16 + 2 seconds, plus setup/build/export time.
Its single Windows job has a 15-minute ceiling. Longer workloads and matrices
increase billed runner time; select duration and concurrency for the evidence
window you actually need. A kill-on-close Windows job contains this helper and
its descendants, including command-timeout failures. Normal reports still
distinguish successful cleanup from incomplete cleanup; hard runner loss cannot
create a confirmed final report.

## Verification record

Native Windows checks on 2026-10-31 used a clean isolated source archive/wheel
from the v5.4.0 candidate, outside its checkout. Growing evidence returned `1`,
the changed steady workload returned `0`, its strict comparison was comparable
with memory resolved, and the valid two-second/zero-warm-up run returned `3`.
All three exported matching JSON/HTML files, machine summaries and confirmed
zero remaining owned processes. The first short fixture was rejected because
its two-second warm-up equaled its duration; the corrected fixture explicitly
uses zero warm-up and tests the intended inconclusive policy.

The [hosted Windows run](https://github.com/JunaidAhamed-7777/DrainTracer/actions/runs/41205949586)
also passed. Its three downloaded artifacts contained 65 growing samples
(exit `1`), 64 steady samples (exit `0`), and eight short-run samples (exit `3`,
no judged window). The strict comparison was comparable and improved, with
memory resolved. Every artifact retained the matching JSON/HTML pair, machine
summary and zero remaining owned processes. Technical fixture validation does
not establish user-pilot acceptance or a production workload's detection accuracy.
