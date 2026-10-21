"""Focused dashboard views: navigation, layout at each size, and legacy consoles."""

from __future__ import annotations

import unittest
from dataclasses import replace

from helpers import make_console, make_denied, make_process, render_text, sample_trees

from src.dashboard import DashboardApp
from src.keys import DOWN, ESCAPE

MB = 1024 * 1024
# The sizes every view must fill exactly, and the legacy code page to check.
SIZES = ((80, 24), (120, 40))
LEGACY = "cp437"


def _app(width: int = 120, height: int = 40, encoding: str = "utf-8") -> DashboardApp:
    return DashboardApp("C:/targets/worker.exe", console=make_console(width, height, encoding))


def _worker(pid: int, private_mb: float = 10, name: str = "worker.exe", ppid: int = 100):
    process = make_process(pid, ppid=ppid, name=name, private=int(private_mb * MB))
    return replace(process, create_time=float(pid))


def growing_session(app: DashboardApp, samples: int = 25) -> None:
    """A root, a growing worker, a steady one that exits, and a denied process."""
    root = replace(make_process(100, ppid=1, name="parent.exe"), create_time=1.0)
    trees = []
    for index in range(samples):
        tree = [root, _worker(2, 30 + index * 4)]
        if index < samples - 3:
            tree.append(_worker(1, 10))
        tree.append(replace(make_denied(7, ppid=100), create_time=7.0))
        trees.append(tree)
    sample_trees(app, trees)


class ViewRenderingMixin:
    """Render one view at every size and on a legacy console."""

    VIEW_KEY = ""

    def prepare(self, app: DashboardApp) -> None:
        growing_session(app)

    def rendered(self, width: int, height: int, encoding: str = "utf-8") -> str:
        app = _app(width, height, encoding)
        self.prepare(app)
        app._handle_key(self.VIEW_KEY)
        return render_text(app)

    def assert_fills(self, output: str, width: int, height: int) -> None:
        lines = output.splitlines()
        self.assertEqual(len(lines), height)
        self.assertTrue(all(len(line) <= width for line in lines), max(map(len, lines)))

    def test_fills_each_size_and_legacy_consoles(self) -> None:
        for width, height in SIZES:
            for encoding in ("utf-8", LEGACY):
                with self.subTest(width=width, height=height, encoding=encoding):
                    output = self.rendered(width, height, encoding)
                    self.assert_fills(output, width, height)
                    if encoding == LEGACY:
                        output.encode(LEGACY)


class NavigationTests(unittest.TestCase):
    def test_number_keys_switch_views_and_escape_returns(self) -> None:
        app = _app()
        app._handle_key("5")
        self.assertEqual(app.view, "monitor")
        app._handle_key(ESCAPE)
        self.assertEqual(app.view, "overview")
        app._handle_key("9")  # not a view
        self.assertEqual(app.view, "overview")

    def test_escape_closes_help_before_leaving_the_view(self) -> None:
        app = _app()
        app._handle_key("5")
        app._handle_key("?")
        app._handle_key("\x1b")
        self.assertFalse(app._show_help)
        self.assertEqual(app.view, "monitor")
        app._handle_key("\x1b")
        self.assertEqual(app.view, "overview")

    def test_switching_views_leaves_monitoring_alone(self) -> None:
        app = _app()
        app._handle_key("5")
        app._handle_key(DOWN)
        self.assertFalse(app.detector.is_paused)
        self.assertEqual(app._sort_mode, "tree")

    def test_overview_footer_offers_views_and_focused_views_show_tabs(self) -> None:
        app = _app(130, 40)
        growing_session(app, 5)
        self.assertIn("views", render_text(app))
        app._handle_key("5")
        output = render_text(app)
        self.assertIn("5 MONITOR", output)
        self.assertIn("1 Overview", output)
        self.assertIn("overview", output.splitlines()[-1])

    def test_compact_views_use_one_status_line(self) -> None:
        app = _app(80, 24)
        growing_session(app, 5)
        app._handle_key("5")
        lines = render_text(app).splitlines()
        self.assertIn("5 MONITOR", lines[0])
        self.assertIn(app._status(app.snapshot)[0], lines[0])
        self.assertNotIn("PATH", "".join(lines))  # no full header at 80x24

    def test_legacy_consoles_replace_unprintable_names_and_ellipses(self) -> None:
        app = _app(80, 24, LEGACY)
        long_name = "工作-" + "w" * 60 + "✓.exe"
        sample_trees(app, [[make_process(100, ppid=1, name=long_name)]] * 3)
        for key in ("1", "5"):
            app._handle_key(key)
            output = render_text(app)
            output.encode(LEGACY)
            self.assertEqual(len(output.splitlines()), 24)
            self.assertTrue(all(len(line) <= 80 for line in output.splitlines()))

    def test_help_scrolls_on_small_terminals(self) -> None:
        app = _app(80, 24)
        growing_session(app, 3)
        app._handle_key("?")
        first = render_text(app)
        self.assertIn("more lines", first)
        self.assertNotIn("MEMORY COUNTERS", first)
        app._handle_key("end")
        last = render_text(app)
        self.assertIn("MEMORY COUNTERS", last)
        self.assertIn("end of help", last)
        self.assertEqual(len(last.splitlines()), 24)
        app._handle_key("home")
        self.assertEqual(app._help_offset, 0)
        app._handle_key("?")
        app._handle_key("?")
        self.assertEqual(app._help_offset, 0)  # reopening starts at the top

    def test_help_lists_the_views(self) -> None:
        app = _app(120, 60)
        app._handle_key("?")
        output = render_text(app)
        self.assertIn("VIEWS", output)
        self.assertIn("Monitor: sampling cadence", output)


class MonitorViewTests(ViewRenderingMixin, unittest.TestCase):
    VIEW_KEY = "5"

    def test_shows_cadence_cost_coverage_and_missing_host_context(self) -> None:
        output = " ".join(self.rendered(120, 40).split())
        self.assertIn("MONITOR DIAGNOSTICS", output)
        self.assertIn("requested 500 ms", output)
        self.assertIn("actual 500 ms (+0 ms)", output)
        self.assertIn("PID 7 access denied", output)
        self.assertIn("1 process unreadable (PID 7)", output)
        self.assertIn("handle types unavailable", output)
        self.assertIn("not collected", output)
        self.assertIn("25 collected", output)

    def test_before_any_sample(self) -> None:
        app = _app(80, 24)
        app._handle_key("5")
        output = render_text(app)
        self.assert_fills(output, 80, 24)
        self.assertIn("none yet", output)


if __name__ == "__main__":
    unittest.main()
