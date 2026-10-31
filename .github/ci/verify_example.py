"""Assert native workflow evidence rather than parsing terminal text."""

import json
import os
import re
from pathlib import Path

expected_version = os.environ["EXPECTED_VERSION"]
assert re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", expected_version), "Use an exact expected package version"
roots = {name: Path(os.environ[name]) for name in ("BEFORE", "AFTER", "SHORT")}
results = {name: json.loads((path / "result.json").read_text(encoding="utf-8")) for name, path in roots.items()}
assert results["BEFORE"]["run_exit"] == 1, results["BEFORE"]
assert results["AFTER"]["run_exit"] == 0, results["AFTER"]
assert results["SHORT"]["run_exit"] == 3, results["SHORT"]
comparison = json.loads((roots["AFTER"] / "comparison.json").read_text(encoding="utf-8"))
assert comparison["comparability"] == "comparable", comparison
assert comparison["resources"]["memory"]["signal"] == "resolved", comparison
for name, result in results.items():
    report = Path(result["report_file"])
    assert report.is_file() and report.with_suffix(".html").is_file(), name
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data.get("version") == f"v{expected_version}", f"{name} runtime version does not match the requested package"
    assert data["outcome"]["cleanup"]["remaining_count"] == 0, name
    assert (roots[name] / "summary.json").is_file(), name
assert not json.loads(Path(results["SHORT"]["report_file"]).read_text(encoding="utf-8"))["detection_ran"]
print("Requested package version verified in growing, changed and failed-short evidence; memory signal resolved in a strict comparison.")
