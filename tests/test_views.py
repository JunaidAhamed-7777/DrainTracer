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


class ProcessesViewTests(ViewRenderingMixin, unittest.TestCase):
    VIEW_KEY = "2"

    def _app(self, width: int = 120, height: int = 40) -> DashboardApp:
        app = _app(width, height)
        growing_session(app)
        app._handle_key("2")
        return app

    def _pids(self, app: DashboardApp) -> list[int]:
        return [row.pid for row in app._inspector_rows()]

    def _selected(self, app: DashboardApp) -> int:
        return app._selected_row(app._inspector_rows()).pid

    def test_lists_live_then_exited_and_details_the_first(self) -> None:
        app = self._app()
        self.assertEqual(self._pids(app), [100, 2, 7, 1])
        output = render_text(app)
        self.assertIn("PROCESSES 3 live", output)
        self.assertIn("1 exited", output)
        self.assertIn("parent.exe  PID 100", output)
        self.assertIn("Tree", output)
        self.assertIn("kill whole tree", output.splitlines()[-1])

    def test_growth_ranking_follows_the_interval(self) -> None:
        app = self._app()
        app._handle_key("s")
        self.assertEqual(app._proc_order, "private_bytes")
        self.assertEqual(self._pids(app)[0], 2)  # the growing worker leads
        app._handle_key(DOWN)
        app._handle_key(DOWN)
        row = app._selected_row(app._inspector_rows())
        self.assertIsNone(row.growth.get("private_bytes"))  # denied: no measured history
        app._proc_selected = (2, 2.0)
        output = " ".join(render_text(app).split())
        self.assertIn("Growth 10s private +80.0 MB (+8.0 MB/s)", output)
        app._handle_key("right")
        app._handle_key("right")
        app._handle_key("right")
        self.assertEqual(app._proc_interval_label(), "lifetime")
        self.assertIn("Growth lifetime private +96.0 MB", " ".join(render_text(app).split()))
        self.assertEqual(app.view, "processes")
        self.assertEqual(app._sort_mode, "tree")  # S here does not touch the overview sort

    def test_selection_follows_identity_across_refreshes_and_exits(self) -> None:
        app = _app(120, 40)
        root = replace(make_process(100, ppid=1, name="parent.exe"), create_time=1.0)
        sample_trees(app, [[root, _worker(1), _worker(2)]] * 3)
        app._handle_key("2")
        app._handle_key(DOWN)
        self.assertEqual(self._selected(app), 1)
        # PID 1 exits and a new process takes the slot before it in the list.
        sample_trees(app, [[root, _worker(2), _worker(3)]], start=3)
        row = app._selected_row(app._inspector_rows())
        self.assertEqual(row.identity, (1, 1.0))
        self.assertTrue(row.exited)
        output = render_text(app)
        self.assertIn("EXITED", output)
        self.assertIn("exited", output)
        # A reused PID with another creation time is a different process.
        sample_trees(app, [[root, _worker(2), replace(_worker(9), pid=1, create_time=50.0)]],
                     start=4)
        rows = app._inspector_rows()
        self.assertTrue(app._selected_row(rows).exited)

    def test_a_selection_that_left_every_list_says_so(self) -> None:
        app = self._app(80, 24)
        app._proc_selected = (555, 5.0)
        output = render_text(app)
        self.assertIn("PID 555 is no longer tracked", " ".join(output.split()))
        self.assert_fills(output, 80, 24)
        app._handle_key(DOWN)
        self.assertEqual(self._selected(app), 100)

    def test_search_filters_and_captures_letters(self) -> None:
        app = self._app()
        app._handle_key("/")
        for key in "work":
            app._handle_key(key)
        self.assertEqual(self._pids(app), [2, 1])
        app._handle_key("q")  # typed into the filter, not a quit
        self.assertFalse(app._quit_requested)
        self.assertEqual(self._pids(app), [])
        self.assertIn("No process matches 'workq'", render_text(app))
        app._handle_key("backspace")
        app._handle_key("enter")
        self.assertEqual(self._pids(app), [2, 1])
        app._handle_key("7")  # back to normal keys: 7 is not a view
        self.assertEqual(app.view, "processes")
        app._handle_key(ESCAPE)  # clears the filter first
        self.assertEqual(app.view, "processes")
        self.assertEqual(len(self._pids(app)), 4)
        app._handle_key(ESCAPE)
        self.assertEqual(app.view, "overview")

    def test_pid_prefix_search(self) -> None:
        app = self._app()
        app._proc_query = "10"
        self.assertEqual(self._pids(app), [100])

    def test_kill_always_targets_the_whole_tree(self) -> None:
        app = self._app()
        app._handle_key(DOWN)
        self.assertEqual(self._selected(app), 2)
        app.manager._tracked_pids = {100, 2}
        calls = []
        app.manager.kill = lambda *args, **kwargs: calls.append((args, kwargs))
        app._handle_key("k")
        self.assertEqual(calls, [((), {})])  # the tree-wide kill, no PID argument

    def test_detail_focus_scrolls_and_enter_expands(self) -> None:
        app = self._app(80, 24)
        app._handle_key("tab")
        self.assertEqual(app._proc_focus, "detail")
        app._handle_key(DOWN)
        self.assertEqual(self._selected(app), 100)  # the list did not move
        app._handle_key("tab")
        app._handle_key("enter")
        output = render_text(app)
        self.assertTrue(app._proc_expanded)
        self.assertNotIn("PROCESSES 3 live", output)
        self.assertIn("SELECTED PROCESS", output)
        self.assert_fills(output, 80, 24)

    def test_denied_and_long_names_render(self) -> None:
        app = _app(80, 24, LEGACY)
        long_name = "a-very-long-process-name-" * 4 + ".exe"
        sample_trees(app, [[make_process(100, ppid=1, name=long_name), make_denied(7, ppid=100)]] * 3)
        app._handle_key("2")
        app._handle_key("end")
        output = render_text(app)
        output.encode(LEGACY)
        self.assert_fills(output, 80, 24)
        self.assertIn("access denied", " ".join(output.split()))

    def test_long_lists_scroll_to_keep_the_selection_visible(self) -> None:
        app = _app(80, 24)
        root = make_process(100, ppid=1, name="parent.exe")
        workers = [_worker(1000 + n) for n in range(30)]
        sample_trees(app, [[root, *workers]] * 2)
        app._handle_key("2")
        app._handle_key("end")
        output = render_text(app)
        self.assertIn("1029", output)
        self.assertIn("of 31", output)


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
