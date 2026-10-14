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

    def test_a_failed_sample_neither_clears_signals_nor_adds_history(self) -> None:
        app = _app()
        alert = make_alert(LeakResource.MEMORY, LeakSeverity.HIGH)
        app._accept_snapshot(make_snapshot(alerts=(alert,)))
        app._accept_snapshot(make_snapshot(metadata={"sampling_error": "access denied"}))
        app._accept_snapshot(make_snapshot(alerts=(alert,)))
        messages = self._messages(app)
        self.assertEqual(len([m for m in messages if "leak signal" in m]), 1)
        self.assertFalse(any("cleared" in message for message in messages))
        self.assertEqual(messages.count("Sampling error: access denied"), 1)
        self.assertEqual(app._sample_count, 2)
        self.assertEqual(len(app._history["rss_bytes"]), 2)

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

    def test_summary_names_signals_outcome_and_settings(self) -> None:
        from dataclasses import replace
        from pathlib import Path

        from src.config import MonitorSettings

        settings = replace(MonitorSettings(), warmup=5.0, profile="ci",
                           source=Path("C:/p/.drainrc"))
        app = DashboardApp("C:/targets/worker.exe", console=make_console(), settings=settings)
        alert = replace(make_alert(LeakResource.HANDLES, LeakSeverity.MEDIUM),
                        growth_by_type={"socket": 40})
        app._accept_snapshot(make_snapshot(alerts=(alert,)))
        app._stopped_target = True
        with app.console.capture() as capture:
            app.console.print(app.render_summary())
        output = capture.get()
        self.assertIn("worker.exe · PID 100 · launched, stopped by DrainTracer", output)
        self.assertIn("memory clear · handles MEDIUM (socket +40) · threads clear", output)
        self.assertIn("5s · did not finish", output)
        self.assertIn("profile ci from", output)

    def test_summary_folds_long_report_paths_instead_of_cutting_them(self) -> None:
        from pathlib import Path

        app = _app(60, 40)
        app._accept_snapshot(make_snapshot())
        app._last_report = Path("C:/very/long/directory/name/for/reports/leak_report_"
                                "11111111-2222-4333-8444-555555555555.json")
        app._last_report_id = "11111111-2222-4333-8444-555555555555"
        with app.console.capture() as capture:
            app.console.print(app.render_summary())
        joined = "".join(line.strip("│ ") for line in capture.get().splitlines())
        self.assertIn("555555555555.json", joined)
        self.assertNotIn("…", capture.get())

    def test_bracketed_names_are_shown_literally(self) -> None:
        from dataclasses import replace
        from pathlib import Path

        from src.config import MonitorSettings

        settings = replace(MonitorSettings(), source=Path("C:/p[red]/.drainrc"))
        app = DashboardApp("C:/x/app[red].exe", console=make_console(), settings=settings,
                           report_dir="C:/out[bold]")
        app._accept_snapshot(make_snapshot())
        with app.console.capture() as capture:
            app.console.print(app.render_summary())
        summary = capture.get()
        self.assertIn("app[red].exe", summary)
        self.assertIn("p[red]", summary)
        app._handle_key("?")
        with app.console.capture() as capture:
            app.console.print(app._render())
        self.assertIn("out[bold]", capture.get())

    def test_clean_summary_is_inconclusive_until_a_window_was_judged(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot())
        with app.console.capture() as capture:
            app.console.print(app.render_summary())
        self.assertIn("inconclusive: monitoring ended before the 10s window filled", capture.get())
        app.detector._detection_started = True
        with app.console.capture() as capture:
            app.console.print(app.render_summary())
        self.assertIn("no leak signals observed", capture.get())


class HandleTypeDisplayTests(unittest.TestCase):
    def _types(self, **values: int) -> dict[str, int]:
        from src.handle_types import CATEGORIES

        return {**dict.fromkeys(CATEGORIES, 0), **values}

    def test_card_lists_named_types_before_other(self) -> None:
        from dataclasses import replace

        app = _app()
        snapshot = replace(
            make_snapshot(),
            handle_types=self._types(other=900, file=120, event=80, registry=12, socket=3, thread=2),
        )
        app._accept_snapshot(snapshot)
        self.assertEqual(
            app._handle_breakdown(), "file 120 · event 80 · registry 12 · socket 3 · +2 more"
        )
        with app.console.capture() as capture:
            app.console.print(app._render())
        self.assertIn("file 120", capture.get())

    def test_breakdown_survives_samples_without_classification(self) -> None:
        from dataclasses import replace

        app = _app()
        app._accept_snapshot(replace(make_snapshot(), handle_types=self._types(file=7)))
        app._accept_snapshot(make_snapshot())
        self.assertEqual(app._handle_breakdown(), "file 7")

    def test_without_classification_the_card_shows_per_process_handles(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot())
        self.assertEqual(app._handle_breakdown(), "")
        with app.console.capture() as capture:
            app.console.print(app._render())
        self.assertIn("per process", capture.get())

    def test_handle_signal_events_name_the_growing_type(self) -> None:
        from dataclasses import replace

        app = _app()
        alert = replace(
            make_alert(LeakResource.HANDLES, LeakSeverity.MEDIUM), growth_by_type={"socket": 40}
        )
        app._accept_snapshot(make_snapshot(alerts=(alert,)))
        self.assertTrue(app._events[-1][1].endswith("by type: socket +40"))


class SettingsDisplayTests(unittest.TestCase):
    def test_health_panel_names_the_profile_and_windows(self) -> None:
        from dataclasses import replace

        from src.config import MonitorSettings

        settings = replace(MonitorSettings(), windows=(30.0, 120.0), profile="game")
        app = DashboardApp("C:/targets/worker.exe", console=make_console(), settings=settings)
        app._accept_snapshot(make_snapshot())
        with app.console.capture() as capture:
            app.console.print(app._render())
        self.assertIn("profile game · windows 30s/2m", capture.get())


class WarmupDisplayTests(unittest.TestCase):
    def test_warmup_shows_in_the_status_and_health_panel(self) -> None:
        from dataclasses import replace

        from src.config import MonitorSettings

        for width, height in LayoutTests.SIZES:
            with self.subTest(width=width, height=height):
                app = DashboardApp(
                    "C:/targets/worker.exe",
                    console=make_console(width, height),
                    settings=replace(MonitorSettings(), warmup=12.0),
                )
                app.detector._warmup_elapsed = 4.2
                app._accept_snapshot(replace(make_snapshot(), warming_up=True))
                with app.console.capture() as capture:
                    app.console.print(app._render())
                output = capture.get()
                self.assertIn("WARMING UP", output)
                lines = output.splitlines()
                self.assertEqual(len(lines), height)
                if width >= 96:
                    self.assertIn("detection in 8s", output)

    def test_status_returns_to_running_after_the_warmup(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot())
        self.assertEqual(app._status(app.snapshot)[0], "RUNNING")
        self.assertEqual(DashboardApp._countdown(0.2), "1s")
        self.assertEqual(DashboardApp._countdown(64.1), "1m5s")


class HelpAndSortTests(unittest.TestCase):
    def _render(self, app: DashboardApp) -> str:
        with app.console.capture() as capture:
            app.console.print(app._render())
        return capture.get()

    def test_help_toggles_and_escape_closes_it(self) -> None:
        app = _app()
        app._accept_snapshot(make_snapshot())
        app._handle_key("?")
        output = self._render(app)
        self.assertIn("HELP", output)
        self.assertIn("READING A SIGNAL", output)
        self.assertIn("built-in defaults", output)
        app._handle_key("\x1b")
        self.assertNotIn("READING A SIGNAL", self._render(app))
        app._handle_key("H")
        app._handle_key("h")
        self.assertFalse(app._show_help)

    def test_help_fits_every_breakpoint(self) -> None:
        for width, height in LayoutTests.SIZES:
            with self.subTest(width=width, height=height):
                app = _app(width, height)
                app._accept_snapshot(make_snapshot())
                app._handle_key("?")
                lines = self._render(app).splitlines()
                self.assertEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))

    def test_sort_cycles_through_metrics(self) -> None:
        from dataclasses import replace

        app = _app()
        processes = (
            make_process(100, ppid=1, rss=10 * 1024 * 1024, handles=900),
            replace(make_process(200, ppid=100, rss=90 * 1024 * 1024, handles=50), cpu_percent=7.0),
            make_process(300, ppid=100, rss=40 * 1024 * 1024, handles=400),
        )
        app._accept_snapshot(make_snapshot(processes=processes, root_pid=100))

        def order() -> list[int]:
            return [proc.pid for proc, _ in app._ordered_processes(app.snapshot)]

        self.assertEqual(order(), [100, 200, 300])
        app._handle_key("s")
        self.assertEqual(app._sort_mode, "memory")
        self.assertEqual(order(), [200, 300, 100])
        self.assertIn("by memory", self._render(app))
        app._handle_key("S")
        self.assertEqual(order(), [100, 300, 200])
        app._handle_key("s")
        self.assertEqual(order()[0], 200)  # cpu
        app._handle_key("s")
        self.assertEqual(app._sort_mode, "tree")


class EventLogTests(unittest.TestCase):
    def _lines(self, app: DashboardApp, width: int, height: int) -> list[str]:
        with app.console.capture() as capture:
            app.console.print(app._event_table(width, height))
        return capture.get().splitlines()

    def test_long_messages_wrap_onto_two_lines(self) -> None:
        app = _app()
        app._append_event("short", "info")
        app._append_event("word " * 12 + "end", "info")
        lines = self._lines(app, 40, 10)
        self.assertEqual(len(lines), 3)  # one line, then a two-line message
        self.assertIn("short", lines[0])
        self.assertTrue(lines[2].rstrip().endswith("…"))

    def test_the_newest_events_win_when_space_runs_out(self) -> None:
        app = _app()
        for index in range(10):
            app._append_event(f"event {index}", "info")
        lines = self._lines(app, 40, 3)
        self.assertEqual(len(lines), 3)
        self.assertIn("event 9", lines[-1])
        self.assertIn("event 7", lines[0])

    def test_a_long_newest_event_fits_a_one_line_budget(self) -> None:
        app = _app()
        app._append_event("word " * 30, "warn")
        self.assertEqual(len(self._lines(app, 40, 1)), 1)


class HealthDetailTests(unittest.TestCase):
    def test_type_attribution_comes_before_the_rate(self) -> None:
        from dataclasses import replace

        app = _app()
        alert = replace(
            make_alert(LeakResource.HANDLES, LeakSeverity.HIGH),
            growth_by_type={"file": 340, "event": 2},
        )
        self.assertEqual(app._health_detail(alert), "+10 in 10s · file +340 · 1.0/s")
        self.assertEqual(app._health_detail(make_alert()), "+10.0 B in 10s · 1.0 B/s")


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

    def test_pause_and_kill_are_unavailable_when_the_tree_contains_draintracer(self) -> None:
        from unittest import mock

        app, calls = self._attached_app()
        pauses: list[str] = []
        app.manager.pause = lambda: pauses.append("pause") or True  # type: ignore[method-assign]
        with mock.patch.object(type(app.manager), "contains_monitor",
                               new_callable=mock.PropertyMock, return_value=True):
            for key in ("k", "k", " "):
                app._handle_key(key)
            with app.console.capture() as capture:
                app.console.print(app._render_footer())
        self.assertEqual((calls, pauses), ([], []))
        self.assertEqual(
            [message for _s, message, _l in app._events],
            ["Kill is unavailable: DrainTracer runs inside this process tree"] * 2
            + ["Pause is unavailable: DrainTracer runs inside this process tree"],
        )
        self.assertNotIn("pause", capture.get())
        self.assertNotIn("kill", capture.get())

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
            self.assertIn("(JSON + HTML)", saved[-1])

            pages = sorted(report_dir.glob("leak_report_*.html"))
            self.assertEqual([page.stem for page in pages], [path.stem for path in files])
            self.assertEqual(app._last_html_report, report_dir / f"{app._last_report.stem}.html")

    def test_html_failure_keeps_the_json_report(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            app = DashboardApp("C:/targets/worker.exe", console=make_console(), report_dir=temp)
            app._accept_snapshot(make_snapshot())
            with mock.patch("src.session.write_html_report", side_effect=RuntimeError("boom")):
                app._handle_key("e")
            self.assertTrue(app._last_report.is_file())
            self.assertIsNone(app._last_html_report)
            self.assertEqual(list(Path(temp).glob("*.html")), [])
            messages = [message for _s, message, _l in app._events]
            self.assertIn("HTML report failed: boom", messages)
            self.assertTrue(messages[-1].endswith("(JSON)"))


class ExportFailureTests(unittest.TestCase):
    def test_any_export_failure_is_logged_not_raised(self) -> None:
        from unittest import mock

        app = _app()
        app._accept_snapshot(make_snapshot())
        with mock.patch.object(app, "export_log", side_effect=RuntimeError("bug")):
            app._handle_key("e")
        self.assertEqual(app._events[-1][1], "Export failed: bug")


class OpenReportTests(unittest.TestCase):
    def test_open_without_a_report_explains_how_to_export(self) -> None:
        app = _app()
        app._handle_key("o")
        self.assertIn("press E", app._events[-1][1])

    def test_open_launches_the_html_report(self) -> None:
        from pathlib import Path
        from unittest import mock

        app = _app()
        app._last_html_report = Path("C:/reports/leak_report_x.html")
        with mock.patch("src.dashboard.os.startfile", create=True) as startfile, \
                mock.patch("src.dashboard.os.name", "nt"):
            app._handle_key("O")
        startfile.assert_called_once_with(app._last_html_report)
        self.assertEqual(app._events[-1][1], "Opened leak_report_x.html")

    def test_open_failure_is_logged(self) -> None:
        from pathlib import Path
        from unittest import mock

        app = _app()
        app._last_html_report = Path("C:/reports/leak_report_x.html")
        with mock.patch("src.dashboard.os.startfile", create=True, side_effect=OSError("denied")), \
                mock.patch("src.dashboard.os.name", "nt"):
            app._handle_key("o")
        self.assertIn("Could not open leak_report_x.html: denied", app._events[-1][1])

    def test_footer_offers_open_only_after_an_html_export(self) -> None:
        from pathlib import Path

        app = _app()
        app._accept_snapshot(make_snapshot())
        with app.console.capture() as capture:
            app.console.print(app._render())
        self.assertNotIn("open report", capture.get())
        app._last_html_report = Path("C:/reports/leak_report_x.html")
        with app.console.capture() as capture:
            app.console.print(app._render())
        self.assertIn("open report", capture.get())


if __name__ == "__main__":
    unittest.main()
