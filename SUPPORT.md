# Getting help

Start with the [README](README.md), the detailed
[working principle](working-principle.md), and the
[torture workload guide](torture/README.md). The commented
[settings example](examples/drainrc.toml) covers project configuration and
profiles. `python src/main.py --help` lists the monitoring flags;
`python src/main.py diff --help` covers report comparison.

For usage questions, open an
[issue](https://github.com/JunaidAhamed-7777/DrainTracer/issues/new) describing
what you are trying to measure, your Windows and Python versions, the
DrainTracer version or commit, and the command you ran. Search existing
issues first. Use the bug-report form for reproducible failures and the
feature-request form for proposed behavior.

## Include useful evidence

Reduce a problem to a small target when possible. The bounded targets under
`torture/` help distinguish a project issue from workload-specific behavior.
State whether you launched with `--target` or attached with `--pid`, and
whether you used a settings file or profile. Explain the expected result and
the observed result, including the exit code for headless runs.

To capture recovered errors and tracebacks:

```powershell
$env:DRAINTRACER_LOG = "drain.log"
python src/main.py --target .\torture\steady.py
```

Remove the setting after diagnosing the issue if you no longer need it:

```powershell
Remove-Item Env:DRAINTRACER_LOG -ErrorAction SilentlyContinue
```

Review screenshots, reports, and logs before posting. Remove identifying
paths, process details, secrets, and unrelated data. Reports are optional;
share the smallest evidence needed to reproduce the issue.

Follow the [safe sharing guide](docs/privacy.md#before-sharing-evidence).
Editing JSON does not update an already exported HTML companion, and
filenames and CI logs can reveal details independently. Public issues and
attachments are public evidence; do not post private reports to request help.

## Privacy, legal, and licensing requests

Read the [privacy policy](PRIVACY.md), [terms of use](TERMS.md), and
[legal index](LEGAL.md). Use a private contact method published on
[the maintainer's GitHub profile](https://github.com/JunaidAhamed-7777).
If none is available, open an issue titled **Private contact requested**
asking only for a private channel. Do not include personal information or
confidential evidence. The request itself and your GitHub identity are public.

The maintainer cannot access or delete reports stored only on your machine.
For information controlled by GitHub or your organization, use their own
request channels. For evidence sent to the maintainer, identify the relevant
post or correspondence through the private route.

## Understand the result

An alert means sustained resource growth matched a heuristic; it does not
identify an allocation site or prove a leak. A clean run shorter than a full
detection window can be `INCONCLUSIVE`. Startup growth may need `--warmup`,
and a workload with legitimate long-term growth may need adjusted thresholds.
Explain these conditions when asking about an unexpected verdict.

Use the [security policy](SECURITY.md) for suspected vulnerabilities and the
[code of conduct](CODE_OF_CONDUCT.md) for conduct concerns. Support is
community based; no response time or service-level agreement is promised.
