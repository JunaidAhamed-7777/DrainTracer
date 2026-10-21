"""HTML report rendering: escaping, embedded data, CSP, and file output."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import make_alert, make_process, make_snapshot

from src.events import LeakSeverity
from src.html_report import render_html_report, write_html_report
from src.report import snapshot_to_dict

HOSTILE = '</script><img src=x onerror="alert(1)">&'


def _report(**extra) -> dict:
    snapshots = [
        snapshot_to_dict(make_snapshot(timestamp=1000.0 + index, alerts=alerts))
        for index, alerts in enumerate(
            ((), (), (make_alert(severity=LeakSeverity.HIGH),), ())
        )
    ]
    report = {
        "report_id": "11111111-2222-4333-8444-555555555555",
        "report_schema": 2,
        "created_at": "2026-10-14T10:00:05.000+05:30",
        "session_started_at": "2026-10-14T10:00:00.000+05:30",
        "session_ended_at": "2026-10-14T10:00:04.000+05:30",
        "version": "v9.9",
        "target_path": "C:\\targets\\worker.exe",
        "root_pid": 100,
        "mode": "launch",
        "target_exit_code": 0,
        "event_log": [
            {"timestamp": 1000.0, "level": "ok", "message": "Launched worker.exe"},
            {"timestamp": 1002.0, "level": "error", "message": "Memory leak signal HIGH"},
        ],
        "snapshots": snapshots,
    }
    report.update(extra)
    return report


def _data_block(document: str) -> str:
    match = re.search(
        r'<script type="application/json" id="drain-data">(.*?)</script>', document, re.S
    )
    assert match is not None
    return match.group(1)


class RenderTests(unittest.TestCase):
    def test_summary_sections_render(self) -> None:
        document = render_html_report(_report())
        self.assertTrue(document.startswith("<!DOCTYPE html>"))
        self.assertIn("<title>DrainTracer · worker.exe · HIGH</title>", document)
        self.assertIn("HIGH leak signal observed", document)
        self.assertIn("memory reached HIGH", document)
        self.assertIn("Launched worker.exe", document)
        self.assertIn("Exit code", document)
        self.assertIn('data-t="2.000" data-t1="2.000"', document)

    def test_memory_metrics_are_named_private_and_working_set(self) -> None:
        document = render_html_report(_report())
        self.assertIn("Memory · private bytes", document)
        self.assertIn("peak private 10.0 MB · RSS (working set) 10.0 MB", document)
        self.assertIn("<th class=\"num\">Private</th><th class=\"num\">RSS (working set)</th>", document)
        self.assertIn('label: "RSS (working set)"', document)

    def test_hostile_text_is_escaped_everywhere(self) -> None:
        snapshot = snapshot_to_dict(
            make_snapshot(processes=(make_process(100, name=HOSTILE),), timestamp=1000.0)
        )
        document = render_html_report(
            _report(
                target_path=f"C:\\{HOSTILE}",
                event_log=[{"timestamp": 1000.0, "level": HOSTILE, "message": HOSTILE}],
                snapshots=[snapshot],
            )
        )
        self.assertNotIn("<img", document)
        self.assertNotIn('onerror="', document)
        self.assertEqual(document.count("</script>"), 2)  # only the two real closing tags
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;", document)
        # Unknown levels fall back to info instead of becoming a CSS class.
        self.assertIn('class="level level-info"', document)

    def test_embedded_data_round_trips(self) -> None:
        document = render_html_report(
            _report(event_log=[{"timestamp": 1001.0, "level": "info", "message": HOSTILE}])
        )
        block = _data_block(document)
        self.assertNotIn("<", block)
        payload = json.loads(block)
        self.assertEqual(payload["events"][0]["message"], HOSTILE)
        self.assertEqual(payload["t"], [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(payload["markers"][0]["severity"], "high")

    def test_csp_pins_the_inline_style_and_script(self) -> None:
        document = render_html_report(_report())
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', document)
        self.assertIsNotNone(csp)
        policy = html.unescape(csp.group(1))  # browsers decode attribute entities
        self.assertIn("default-src 'none'", policy)
        for tag in ("style", "script"):
            body = re.search(rf"<{tag}>(.*?)</{tag}>", document, re.S).group(1)
            digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", policy)

    def test_handle_types_reach_the_card_chart_and_process_table(self) -> None:
        snapshots = []
        for index in range(3):
            snapshot = snapshot_to_dict(make_snapshot(timestamp=1000.0 + index))
            snapshot["handle_types"] = {"file": 10 + 30 * index, "socket": 4, "other": 50}
            snapshot["processes"][0]["handle_types"] = snapshot["handle_types"]
            snapshots.append(snapshot)
        document = render_html_report(_report(snapshots=snapshots))
        self.assertIn("by type: file +60", document)
        self.assertIn("file 70 · socket 4 · other 50", document)
        payload = json.loads(_data_block(document))
        self.assertEqual(payload["handle_types"]["categories"], ["file", "socket", "other"])

    def test_warmup_and_inconclusive_runs(self) -> None:
        snapshots = []
        for index in range(4):
            snapshot = snapshot_to_dict(make_snapshot(timestamp=1000.0 + index))
            snapshot["warming_up"] = index < 2
            snapshots.append(snapshot)
        finished = _report(
            snapshots=snapshots,
            warmup={"seconds": 2.0, "completed": True},
            detection_ran=True,
        )
        document = render_html_report(finished)
        self.assertIn("<dt>Warm-up</dt><dd>2s</dd>", document)
        self.assertIn("since warm-up", document)
        self.assertEqual(json.loads(_data_block(document))["warmup"], [0.0, 2.0])

        unfinished = _report(
            snapshots=[s | {"warming_up": True} for s in snapshots],
            warmup={"seconds": 30.0, "completed": False},
            detection_ran=False,
        )
        document = render_html_report(unfinished)
        self.assertIn("INCONCLUSIVE", document)
        self.assertIn("The 30s warm-up never finished", document)
        self.assertIn("<dd>30s, did not finish</dd>", document)

        short = _report(detection_ran=False, settings={"windows_seconds": [10.0]})
        short["snapshots"] = [snapshot_to_dict(make_snapshot(timestamp=1000.0))]
        self.assertIn("Monitoring ended before the 10s window filled.", render_html_report(short))
        self.assertNotIn("INCONCLUSIVE", render_html_report(_report()))  # field absent

    def test_growing_processes_are_ranked_with_exited_ones(self) -> None:
        def summary(pid, name, growth, exited_at=None):
            return {
                "pid": pid, "name": name, "first_seen": 1000.0, "exited_at": exited_at,
                "peak": {"private_bytes": 50 * 1024 * 1024},
                "growth": {"private_bytes": growth, "handles": 3, "threads": 0},
            }

        processes = {
            "counts": {"live": 2, "started": 3, "exited": 1, "restarted": 1},
            "live": [summary(1, "steady.exe", 0), summary(2, "helper.exe", 1024 * 1024)],
            "exited": [summary(3, "leaky.exe", 40 * 1024 * 1024, exited_at=1003.0)],
        }
        document = render_html_report(_report(processes=processes))
        self.assertIn("Growing processes", document)
        self.assertIn("3 started, 1 exited, 1 restarted.", document)
        self.assertLess(document.index("leaky.exe"), document.index("helper.exe"))
        self.assertIn("+40.0 MB", document)
        self.assertIn('<td class="num">+00:00:03</td>', document)
        self.assertNotIn("Growing processes", render_html_report(_report()))

    def test_legacy_and_empty_reports_render(self) -> None:
        legacy = render_html_report(
            {"events": ["10:00:00 Launched old.exe"], "snapshots": [], "target_path": "old.exe"}
        )
        self.assertIn("Launched old.exe", legacy)
        self.assertIn("No leak signals", legacy)
        empty = render_html_report({})
        self.assertIn("No events were recorded.", empty)
        self.assertIn("No process measurements were recorded.", empty)
        self.assertNotIn("Monitor diagnostics", legacy)


class SessionFactsRenderingTests(unittest.TestCase):
    def test_thinned_reports_say_so_and_show_exact_facts(self) -> None:
        import json as _json

        from test_session_facts import thinned_session_report

        report, _spike_at, _total = thinned_session_report()
        stride = report["snapshot_stride"]
        document = render_html_report(report)
        text = html.unescape(document)
        self.assertIn(f"chart thinned 1:{stride} (peaks, signals and counts are exact)", text)
        self.assertIn("peak private 600.0 MB at +00:01:17", text)  # the spike, 77.5 s in
        self.assertIn("30 signal episodes, 00:00:30 signalled", text)
        self.assertIn("episodes counted during the session are listed", text)
        self.assertIn("Monitor diagnostics", text)
        self.assertIn("500 ms requested · 500 ms actual mean", text)
        self.assertIn("8.5 ms mean · 13.0 ms max", text)
        self.assertIn("Detection evaluated", text)
        _json.loads(_data_block(document))

    def test_thinned_reports_without_facts_are_marked_approximate(self) -> None:
        report = _report(snapshot_stride=8)
        text = html.unescape(render_html_report(report))
        self.assertIn("chart thinned 1:8 (peaks and signal episodes come from the kept samples only)",
                      text)
        self.assertNotIn("Monitor diagnostics", text)


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class ScriptSyntaxTests(unittest.TestCase):
    def test_chart_script_parses(self) -> None:
        from src.html_report import _SCRIPT

        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "report.js"
            script.write_text(_SCRIPT, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(script)], capture_output=True, text=True, timeout=60
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class WriteTests(unittest.TestCase):
    def test_writes_named_file_with_unix_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = write_html_report(_report(), Path(temp) / "reports")
            self.assertEqual(path.name, "leak_report_11111111-2222-4333-8444-555555555555.html")
            self.assertNotIn(b"\r\n", path.read_bytes())

    def test_unpaired_surrogates_in_names_do_not_lose_the_report(self) -> None:
        snapshot = snapshot_to_dict(
            make_snapshot(processes=(make_process(100, name="bad\udc80name.exe"),), timestamp=1000.0)
        )
        with tempfile.TemporaryDirectory() as temp:
            path = write_html_report(_report(snapshots=[snapshot]), temp)
            self.assertIn("bad?name.exe", path.read_text(encoding="utf-8"))

    def test_requires_a_report_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                write_html_report({"snapshots": []}, temp)


if __name__ == "__main__":
    unittest.main()
