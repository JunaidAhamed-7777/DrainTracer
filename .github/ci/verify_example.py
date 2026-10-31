"""Assert native workflow evidence rather than parsing terminal text."""

import json
import os
from pathlib import Path

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
    assert data["outcome"]["cleanup"]["remaining_count"] == 0, name
    assert (roots[name] / "summary.json").is_file(), name
assert not json.loads(Path(results["SHORT"]["report_file"]).read_text(encoding="utf-8"))["detection_ran"]
print("Growing, changed and failed-short evidence retained; memory signal resolved in a strict comparison.")
