"""Generate bounded, synthetic offline fixtures for manual accessibility checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from helpers import make_alert, make_denied, make_process, make_snapshot
from test_html_report import _report
from test_session_facts import thinned_session_report

from src.dashboard import DashboardApp
from src.html_report import render_html_report
from src.report import snapshot_to_dict


def write_fixtures(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)

    def samples(partial=False, unavailable=False, warming=False):
        result = []
        for index in range(40):
            processes = (make_denied(100),) if unavailable else (
                (make_process(100), make_denied(200)) if partial else (make_process(100),)
            )
            sample = snapshot_to_dict(make_snapshot(timestamp=1000 + index, processes=processes,
                alerts=(make_alert(),) if index == 20 and not warming and not unavailable else (),
                measured_coverage=True))
            sample["warming_up"] = warming
            if not unavailable and index % 5 == 0:
                sample["handle_types"] = {"file": 50 + index, "event": 3}
            result.append(sample)
        return result

    thinned, _spike, _count = thinned_session_report()
    cases = {
        "full": _report(snapshots=samples(), detection_ran=True),
        "partial": _report(snapshots=samples(partial=True), detection_ran=True, coverage={"partial": True}),
        "unavailable": _report(snapshots=samples(unavailable=True), detection_ran=False),
        "warmup": _report(snapshots=samples(warming=True), detection_ran=False,
                          warmup={"seconds": 60, "completed": False}),
        "thinned": thinned,
    }
    written = []
    for name, report in cases.items():
        report["version"] = DashboardApp.VERSION
        path = directory / f"{name}.html"
        path.write_text(render_html_report(report), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    for output in write_fixtures(args.output):
        print(output)
