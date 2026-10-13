"""Rendering and event-log behaviour of the dashboard without live processes."""

from __future__ import annotations

import unittest

from helpers import make_alert, make_console, make_process, make_snapshot

from src.dashboard import DashboardApp
from src.events import LeakResource, LeakSeverity


def _app(width: int = 140, height: int = 40, encoding: str = "utf-8") -> DashboardApp:
    return DashboardApp(
        "C:/targets/worker.exe",
        console=make_console(width, height, encoding),
    )


class TreeOrderTests(unittest.TestCase):
    def test_children_follow_their_parent_with_connectors(self) -> None:
        app = _app()
        snapshot = make_snapshot(
            processes=(
                make_process(300, ppid=200),
                make_process(100, ppid=1),
                make_process(200, ppid=100),
                make_process(201, ppid=100),
            ),
            root_pid=100,
        )
        ordered = app._tree_order(snapshot)
        self.assertEqual([proc.pid for proc, _ in ordered], [100, 200, 300, 201])
        connectors = [connector for _, connector in ordered]
        self.assertEqual(connectors[0], "")
        self.assertEqual(connectors[1], app.glyphs.tree_mid)
        self.assertEqual(connectors[2], app.glyphs.tree_pipe + app.glyphs.tree_last)
        self.assertEqual(connectors[3], app.glyphs.tree_last)

    def test_orphans_are_listed_after_the_root(self) -> None:
        app = _app()
        snapshot = make_snapshot(
            processes=(make_process(500, ppid=999), make_process(100, ppid=1)),
            root_pid=100,
        )
        self.assertEqual([proc.pid for proc, _ in app._tree_order(snapshot)], [100, 500])


class SignalTransitionTests(unittest.TestCase):
    def _messages(self, app: DashboardApp) -> list[str]:
        return [message for _stamp, message, _level in app._events]

    def test_repeated_alerts_log_once(self) -> None:
        app = _app()
        alert = make_alert(LeakResource.MEMORY, LeakSeverity.MEDIUM)
        for _ in range(5):
            app._accept_snapshot(make_snapshot(alerts=(alert,)))
        messages = self._messages(app)
        self.assertEqual(len(messages), 1)
        self.assertIn("Memory leak signal MEDIUM", messages[0])

    def test_escalation_and_clear_are_logged(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.LOW),)))
        app._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        app._accept_snapshot(make_snapshot())
        messages = self._messages(app)
        self.assertEqual(len(messages), 3)
        self.assertIn("escalated to HIGH", messages[1])
        self.assertIn("cleared", messages[2])
        self.assertEqual(app._worst_severity, LeakSeverity.HIGH)

    def test_paused_snapshots_do_not_add_history(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot())
        app._accept_snapshot(make_snapshot(paused=True))
        self.assertEqual(app._sample_count, 1)
        self.assertEqual(len(app._history["rss_bytes"]), 1)


class LayoutTests(unittest.TestCase):
    SIZES = ((150, 45), (140, 26), (96, 32), (66, 24), (50, 20))

    def _render(self, app: DashboardApp) -> str:
        with app.console.capture() as capture:
            app.console.print(app._render())
        return capture.get()

    def test_every_breakpoint_renders_core_sections(self) -> None:
        for width, height in self.SIZES:
            with self.subTest(width=width, height=height):
                app = _app(width, height)
                for index in range(6):
                    app._accept_snapshot(
                        make_snapshot(
                            processes=(
                                make_process(100, ppid=1, rss=(10 + index) * 1024 * 1024),
                                make_process(200, ppid=100),
                            ),
                            alerts=(make_alert(),),
                            timestamp=1000.0 + index,
                        )
                    )
                output = self._render(app)
                lines = output.splitlines()
                self.assertEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))
                self.assertIn("DRAINTRACER", output)
                self.assertIn("LEAK HEALTH", output)
                self.assertIn("EVENT LOG", output)

    def test_legacy_code_page_uses_ascii_only(self) -> None:
        app = _app(120, 40, encoding="cp1252")
        app._accept_snapshot(make_snapshot())
        output = self._render(app)
        output.encode("cp1252")  # must not raise
        self.assertIn("+", output)

    def test_summary_reports_worst_severity(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.CRITICAL),)))
        with app.console.capture() as capture:
            app.console.print(app.render_summary())
        self.assertIn("CRITICAL leak signal observed", capture.get())


class TimeLimitTests(unittest.TestCase):
    def _render(self, app: DashboardApp) -> str:
        with app.console.capture() as capture:
            app.console.print(app._render())
        return capture.get()

    def test_header_counts_down_with_a_time_limit(self) -> None:
        import time

        app = DashboardApp("C:/targets/worker.exe", console=make_console(), timeout=90)
        app._deadline = time.monotonic() + 42.5
        app._accept_snapshot(make_snapshot())
        output = self._render(app)
        self.assertIn("ENDS IN", output)
        self.assertIn("00:00:42", output)

    def test_no_countdown_without_a_time_limit(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot())
        self.assertNotIn("ENDS IN", self._render(app))


class AttachModeTests(unittest.TestCase):
    def _attached_app(self) -> tuple[DashboardApp, list[str]]:
        app = DashboardApp(None, console=make_console(), attach_pid=4242)
        calls: list[str] = []
        app.manager.kill = lambda: calls.append("kill")  # type: ignore[method-assign]
        return app, calls

    def test_kill_needs_a_second_press_when_attached(self) -> None:
        app, calls = self._attached_app()
        app._handle_key("k")
        self.assertEqual(calls, [])
        self.assertIn("Press K again", app._events[-1][1])
        app._handle_key("K")
        self.assertEqual(calls, ["kill"])

    def test_kill_confirmation_expires(self) -> None:
        app, calls = self._attached_app()
        app._handle_key("k")
        app._kill_armed_until = 0.0  # simulate the confirmation window passing
        app._handle_key("k")
        self.assertEqual(calls, [])

    def test_launched_targets_kill_immediately(self) -> None:
        app = _app()
        calls: list[str] = []
        app.manager.kill = lambda: calls.append("kill")  # type: ignore[method-assign]
        app._handle_key("k")
        self.assertEqual(calls, ["kill"])

    def test_attached_labels(self) -> None:
        app, _calls = self._attached_app()
        app._accept_snapshot(make_snapshot(root_pid=4242, processes=(make_process(4242),)))
        with app.console.capture() as capture:
            app.console.print(app._render())
        output = capture.get()
        self.assertIn("4242 (attached)", output)
        self.assertIn("detach & quit", output)


class ExportTests(unittest.TestCase):
    def test_each_export_gets_a_new_uuid_file_in_the_report_dir(self) -> None:
        import json
        import tempfile
        from datetime import datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            report_dir = Path(temp) / "leak_reports"
            app = DashboardApp(
                "C:/targets/worker.exe", console=make_console(), report_dir=report_dir
            )
            app._accept_snapshot(make_snapshot())
            app._handle_key("e")
            app._handle_key("e")

            files = sorted(report_dir.glob("leak_report_*.json"))
            self.assertEqual(len(files), 2)
            ids = set()
            for path in files:
                report = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(path.name, f"leak_report_{report['report_id']}.json")
                self.assertIsNotNone(datetime.fromisoformat(report["created_at"]).tzinfo)
                self.assertEqual(report["version"], DashboardApp.VERSION)
                self.assertEqual(len(report["snapshots"]), 1)
                ids.add(report["report_id"])
            self.assertEqual(len(ids), 2)
            self.assertEqual(app._last_report_id, json.loads(app._last_report.read_text())["report_id"])
            saved = [message for _s, message, _l in app._events if message.startswith("Report ")]
            self.assertEqual(len(saved), 2)
            self.assertIn(app._last_report_id, saved[-1])


if __name__ == "__main__":
    unittest.main()
