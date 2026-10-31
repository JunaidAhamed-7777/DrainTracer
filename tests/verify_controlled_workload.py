"""Installed clean recipes interoperate with continuity-gated local baselines."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def verify_controlled_workload(python: Path, cwd: Path, environment: dict[str, str], source_commit: str):
    target = cwd / "controlled_recipe_steady.py"
    target.write_text("import time; time.sleep(20)\n", encoding="utf-8")
    config = cwd / "controlled_recipe.toml"
    config.write_text('sample_interval="100ms"\nwindows=["1s"]\nwarmup="2s"\n'
                      'fail_on_inconclusive=true\nrequire_full_coverage=true\n'
                      'workload_id="controlled-clean-recipe"\n', encoding="utf-8")
    def run(module, arguments):
        return subprocess.run([str(python), "-m", module, *map(str, arguments)], cwd=cwd, env=environment,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, check=False)
    groups, identities = {}, set()
    quality = []
    for mode, count in (("clean", 4), ("selected", 2), ("inherited", 2)):
        recipe = {"schema": 1, "command": {"target": str(target), "interpreter": str(python)},
                  "timeout": 4, "config": str(config)}
        if mode == "selected":
            recipe["environment"] = {"SAFE_BENCHMARK_FLAG": "selected-example"}
        elif mode == "inherited":
            recipe["inherit_environment"] = True
        recipe_path = cwd / f"controlled-{mode}.json"
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        paths = []
        for index in range(count):
            destination = cwd / f"controlled-{mode}-{index}"
            result = run("draintracer.workload", [recipe_path, "--report-dir", destination, "--no-color"])
            if result.returncode:
                raise RuntimeError(f"Controlled recipe failed({result.returncode}): {result.stdout}{result.stderr}")
            exported = list(destination.rglob("*.json"))
            if len(exported) != 1 or not exported[0].with_suffix(".html").is_file():
                raise RuntimeError("Controlled recipe did not export one JSON/HTML pair")
            data = json.loads(exported[0].read_text(encoding="utf-8"))
            identity = data["report_id"]
            if identity in identities:
                raise RuntimeError("Controlled recipe proof reused a recorded run")
            identities.add(identity)
            monitor, samples = data["session_facts"]["monitor"], data["session_facts"]["samples"]
            counters = (monitor["clock_adjustments"], monitor["sampling_gaps"], samples["paused"])
            if any(type(value) is not int or value != 0 for value in counters):
                raise RuntimeError("Actual recipe continuity loss prevents baseline admission; recollect evidence")
            if (data["outcome"]["cleanup"]["state"] != "complete"
                    or data["outcome"]["cleanup"]["remaining_count"] != 0
                    or data["outcome"]["workload"]["state"] != "complete"):
                raise RuntimeError("Controlled recipe ownership/workflow did not complete")
            quality.append({"mode": mode, "samples": data["sample_count"], "quality": list(counters)})
            paths.append(exported[0])
        groups[mode] = paths
    store = cwd / "controlled-recipe-baseline"
    def arguments(paths):
        return [item for path in paths for item in ("--run", str(path))]
    approved = run("draintracer.baseline_control", ["promote", store, *arguments(groups["clean"][:2]),
                   "--source-commit", source_commit, "--approved-by", "installed-verification",
                   "--reason", "Review independent clean recipe continuity"])
    if approved.returncode or json.loads(approved.stdout).get("revision") != 1:
        raise RuntimeError(f"Clean recipes could not be promoted: {approved.stdout}{approved.stderr}")
    checked = run("draintracer.baseline_control", ["check", store, *arguments(groups["clean"][2:]), "--source-commit", source_commit])
    if checked.returncode not in (0, 3):
        raise RuntimeError(f"Clean recipe check failed unexpectedly: {checked.stdout}{checked.stderr}")
    comparison = json.loads(checked.stdout)["comparison"]
    if (comparison["comparability"] != "comparable" or not comparison["caveats"]
            or any(caveat["kind"] != "clean recipe platform basics" or caveat["blocks"] is not None for caveat in comparison["caveats"])
            or (checked.returncode == 3 and comparison["measured_verdict"] != "inconclusive")):
        raise RuntimeError("Clean recipe lost strict comparison provenance beyond its explicit platform-basics scope")
    before = (store / "baseline.json").read_bytes()
    for mode in ("selected", "inherited"):
        for operation in ("promote", "check"):
            args = [operation, store, *arguments(groups[mode]), "--source-commit", source_commit]
            if operation == "promote":
                args += ["--approved-by", "installed-verification", "--reason", "Must refuse unknown environment values", "--accept-regression"]
            refused = run("draintracer.baseline_control", args)
            if refused.returncode != 3 or refused.stdout or (store / "baseline.json").read_bytes() != before:
                raise RuntimeError("Selected/inherited recipe evidence bypassed a strict approval gate")
    return {"independent_runs": len(identities), "clean_check_code": checked.returncode,
            "clean_verdict": comparison["measured_verdict"], "refused_modes": ["selected", "inherited"], "quality": quality}
