"""Focused dashboard views: navigation, layout at each size, and legacy consoles."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import make_console, make_denied, make_process, render_text, sample_trees

from src.config import resolve_settings
from src.dashboard import DashboardApp
from src.keys import DOWN, ESCAPE, PAGE_DOWN

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

    def test_every_view_survives_resizing(self) -> None:
        app = _app(120, 40)
        growing_session(app)
        for key in "12345":
            app._handle_key(key)
            for width, height in ((200, 60), (80, 24), (50, 20), (66, 24), (120, 40)):
                with self.subTest(view=app.view, width=width, height=height):
                    app.console.size = (width, height)
                    lines = render_text(app).splitlines()
                    self.assertEqual(len(lines), height)
                    self.assertTrue(all(len(line) <= width for line in lines))
                    app._handle_key("?")
                    self.assertEqual(len(render_text(app).splitlines()), height)
                    app._handle_key("?")

    def test_m_adds_a_marker_from_any_view(self) -> None:
        app = _app()
        growing_session(app, 3)
        app._handle_key("m")
        app._handle_key("5")
        app._handle_key("M")
        self.assertEqual([mark.label for mark in app.markers], ["marker 1", "marker 2"])
        self.assertTrue(app._events[-1][1].startswith("Marker 2: marker 2"))
        self.assertFalse(app.detector.is_paused)

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

    def test_profile_budgets_remain_readable_in_scrolled_narrow_legacy_help(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "settings.toml"
            source.write_text('[profiles.ci]\nbudget_private = "500MB"\n'
                              'budget_handles = 2000\nbudget_threads = 64\n', encoding="utf-8")
            settings = resolve_settings(config_path=source, profile="ci", discover=False)
        for width in (40, 80, 120):
            with self.subTest(width=width):
                app = DashboardApp("worker.exe", settings=settings,
                                   console=make_console(width, 24, LEGACY))
                app._handle_key("?")
                pages = []
                for _ in range(30):
                    output = render_text(app)
                    output.encode(LEGACY)
                    self.assertEqual(len(output.splitlines()), 24)
                    self.assertTrue(all(len(line) <= width for line in output.splitlines()))
                    pages.append(output)
                    if "end of help" in output:
                        break
                    app._handle_key(PAGE_DOWN)
                text = " ".join(" ".join(line.strip(" |") for page in pages
                                         for line in page.splitlines()).split())
                for value in ("500.0 MB", "2,000", "64", "Budgets", "without enforcing them"):
                    self.assertIn(value, text)
                self.assertFalse(app.detector.is_paused)

    def test_help_names_absent_budgets_without_zero_ceilings(self) -> None:
        app = _app(120, 100)
        app._handle_key("?")
        output = render_text(app)
        self.assertIn("none configured", output)
        self.assertNotIn("Budget checks", output)


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

    def test_growth_ends_at_the_displayed_sample(self) -> None:
        from unittest import mock

        app = self._app()
        displayed = app._inspector_rows()[1].growth["private_bytes"].delta
        # The detector samples again, but the UI has not accepted that snapshot yet.
        root = replace(make_process(100, ppid=1, name="parent.exe"), create_time=1.0)
        with mock.patch.object(app.detector, "_sample_processes",
                               return_value=[root, _worker(2, 900)]), \
                mock.patch("src.leak_detector.time.time", return_value=1000.0 + 25 * 0.5):
            app.detector._collect_snapshot()
        app._proc_cache = None
        self.assertEqual(app._inspector_rows()[1].growth["private_bytes"].delta, displayed)

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

    def test_parent_names_render_as_literal_text(self) -> None:
        for name in ("[red]worker.exe", "[link=https://example.test]parent[/link]", "[/bold]"):
            with self.subTest(name=name):
                app = _app(140, 60)
                sample_trees(app, [[make_process(100, name=name), _worker(2)]] * 2)
                app._proc_selected = (2, 2.0)
                parent = app._parent_text(100)
                self.assertEqual(parent.plain, f"PID 100 {name}")
                self.assertEqual(parent.spans, [])
                with app.console.capture() as capture:
                    app.console.print(app._render_process_detail(app._selected_row(app._inspector_rows())))
                self.assertIn(f"PID 100 {name}", " ".join(capture.get().split()))

    def test_filter_hides_a_retained_selection_without_claiming_it_exited(self) -> None:
        for identity in ((100, 1.0), (1, 1.0)):
            with self.subTest(identity=identity):
                app = self._app(80, 24)
                app._proc_selected = identity  # A live parent or retained exited worker.
                app._proc_query = "no-match"
                text = " ".join(render_text(app).split())
                self.assertIn(f"Selected PID {identity[0]} is hidden by the search filter", text)
                self.assertNotIn("no longer tracked", text)
                app._handle_key(ESCAPE)
                self.assertEqual(app._selected_row(app._inspector_rows()).identity, identity)

    def test_filter_does_not_hide_the_explanation_for_a_dropped_identity(self) -> None:
        app = self._app()
        app._proc_selected = (555, 5.0)
        app._proc_query = "no-match"
        self.assertIn("PID 555 is no longer tracked", " ".join(render_text(app).split()))

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


class EvidenceViewTests(ViewRenderingMixin, unittest.TestCase):
    VIEW_KEY = "3"

    def _app(self, width: int = 140, height: int = 60, samples: int = 25) -> DashboardApp:
        app = _app(width, height)
        growing_session(app, samples)
        app._handle_key("3")
        return app

    def _text(self, app: DashboardApp) -> str:
        app._handle_key("end")
        tail = render_text(app)
        app._handle_key("home")
        return " ".join((render_text(app) + tail).split())

    def test_opens_on_the_active_signal_with_every_window(self) -> None:
        app = self._app()
        self.assertEqual(app.evidence_resource.value, "memory")
        output = self._text(app)
        self.assertIn("SIGNAL EVIDENCE", output)
        self.assertIn("private bytes, summed over the processes measured", output)
        self.assertIn("signal (heuristic) from the 10s window", output)
        self.assertIn("qualified: HIGH (reported)", output)
        self.assertIn("filling: 12s/25.5s", output)  # the 30s window is still filling
        # PID 1 exits inside the window, so the total dips once: 95% and 10%.
        self.assertIn("Why: the 10s window qualified with slope +7.3 MB/s", output)
        self.assertIn("95% of steps not falling (needs 65%)", output)
        self.assertIn("10% below the running peak", output)
        self.assertIn("Contributors: worker.exe PID 2 +80.0 MB", output)
        self.assertIn("not a probability", output)
        self.assertNotIn("probability of", output)

    def test_values_match_the_detector_evaluations(self) -> None:
        app = self._app()
        evidence = app.snapshot.evidence_for(app.evidence_resource)
        window = evidence.windows[0]
        output = self._text(app)
        self.assertIn(f"{window.score:.2f}", output)
        self.assertIn(f"{window.monotonic_ratio:.0%}", output)

    def test_current_totals_preserve_unknown_zero_and_partial_coverage(self) -> None:
        from src.events import LeakResource

        zero = replace(make_process(100, private=0, handles=0, threads=0), rss_bytes=0)
        cases = (
            ([make_denied(100)], ("n/a", "n/a", "n/a")),
            ([zero], ("0.0 B", "0", "0")),
            ([zero, make_denied(200)], ("0.0 B (partial lower bound)",
                                      "0 (partial lower bound)", "0 (partial lower bound)")),
        )
        for processes, expected in cases:
            with self.subTest(expected=expected):
                app = _app(140, 60)
                sample_trees(app, [processes] * 2)
                for resource, quantity in zip(LeakResource, expected):
                    facts = app._evidence_facts(resource, app.snapshot.evidence_for(resource), None)
                    with app.console.capture() as capture:
                        app.console.print(facts)
                    self.assertIn("Current " + quantity, " ".join(capture.get().split()))
                row = app._inspector_rows()[0]
                tree = app._tree_context(row, " | ")
                for label, quantity in zip(("private", "handles", "threads"), expected):
                    self.assertIn(f"{label} {quantity}", tree)

    def test_process_share_uses_the_measured_partial_total(self) -> None:
        app = _app()
        sample_trees(app, [[make_process(100, private=MB), make_denied(200)]] * 2)
        tree = app._tree_context(app._inspector_rows()[0], " | ")
        self.assertIn("private 1.0 MB (partial lower bound)", tree)
        self.assertIn("this process 100% of measured private", tree)

    def test_steady_resources_explain_which_check_failed(self) -> None:
        app = self._app()
        app._handle_key("right")
        self.assertEqual(app.evidence_resource.value, "handles")
        output = self._text(app)
        self.assertIn("STEADY", output)
        self.assertIn("no upward trend", output)
        self.assertIn("Why not: the longest judged window (10s) did not qualify", output)
        app._handle_key("left")
        app._handle_key("left")
        self.assertEqual(app.evidence_resource.value, "threads")
        # A manual choice sticks when the view is reopened.
        app._handle_key("1")
        app._handle_key("3")
        self.assertEqual(app.evidence_resource.value, "threads")

    def test_collecting_and_unavailable_evidence(self) -> None:
        short = self._app(samples=3)
        output = self._text(short)
        self.assertIn("COLLECT", output)
        self.assertIn("filling: 3/4 points", output)
        self.assertIn("No window holds enough samples yet", output)
        blocked = _app(120, 40)
        sample_trees(blocked, [[make_denied(100)]] * 5)
        blocked._handle_key("3")
        output = self._text(blocked)
        self.assertIn("Unavailable: no live process could be read", output)
        exited = _app(120, 40)
        sample_trees(exited, [[make_process(100)]] * 5 + [[]] * 2)
        exited._handle_key("3")
        output = self._text(exited)
        self.assertIn("Unavailable: the monitored tree has no live process", output)
        exited._handle_key("5")
        self.assertIn("nothing to evaluate: no live process", self._text(exited))

    def test_narrow_tables_keep_the_decisive_columns(self) -> None:
        app = self._app(80, 24)
        output = self._text(app)
        for column in ("WINDOW", "DELTA", "SLOPE", "MONO", "DROPS", "RESULT"):
            self.assertIn(column, output)
        self.assertNotIn("BASELINE", output)
        wide = self._text(self._app(140, 60))
        self.assertIn("BASELINE", wide)
        self.assertIn("SCORE", wide)


class TimelineHelperTests(unittest.TestCase):
    def test_columns_keep_the_highest_sample_and_show_pauses_as_gaps(self) -> None:
        from src.view_timeline import bucket_columns

        points = [(float(t), float(t)) for t in range(10)] + [(30.0, 5.0), (31.0, 6.0)]
        columns, per_column = bucket_columns(points, 0.0, 31.0, 31, gap_seconds=1.5)
        self.assertEqual(columns[0], 0.0)
        self.assertEqual(columns[9], 9.0)
        self.assertIsNone(columns[15])  # the 20 s pause is a gap, not a flat line
        self.assertEqual(columns[-1], 6.0)
        self.assertLess(per_column, 1.2)
        dense, per_column = bucket_columns([(t / 4, float(t)) for t in range(40)], 0.0, 9.75, 10, 1.0)
        self.assertEqual(dense[0], 3.0)  # max of the four samples in the first column
        self.assertAlmostEqual(per_column, 4.0)

    def test_short_gaps_repeat_the_previous_value(self) -> None:
        from src.view_timeline import bucket_columns

        columns, _ = bucket_columns([(0.0, 1.0), (1.0, 2.0)], 0.0, 1.0, 8, gap_seconds=1.5)
        self.assertTrue(all(value is not None for value in columns))

    def test_mark_row_places_symbols_and_merges_collisions(self) -> None:
        from src.events import TimelineKind, TimelineMark
        from src.view_timeline import mark_row

        marks = [
            TimelineMark(0.0, TimelineKind.STARTED, "a"),
            TimelineMark(5.0, TimelineKind.MARKER, "marker 3", index=3),
            TimelineMark(5.1, TimelineKind.EXITED, "b"),
            TimelineMark(99.0, TimelineKind.PAUSED, "outside"),
        ]
        row = mark_row(marks, 0.0, 10.0, 10, window_start=9.0)
        self.assertEqual(row, "+    *   [")


class TimelineViewTests(ViewRenderingMixin, unittest.TestCase):
    VIEW_KEY = "4"

    def _app(self, width: int = 120, height: int = 40) -> DashboardApp:
        app = _app(width, height)
        growing_session(app)
        app._handle_key("4")
        return app

    def test_charts_private_bytes_with_annotations_and_decimation(self) -> None:
        app = self._app()
        output = render_text(app)
        self.assertIn("TIMELINE  PRIVATE BYTES", output)
        self.assertIn("25 samples over 12s", output)
        self.assertIn("one sample per column at most", output)
        self.assertIn("10s window start, baseline", output)
        self.assertIn("x exit", output)
        self.assertIn("! signal", output)
        self.assertIn("worker.exe PID 1 exited", output)
        self.assertIn("markers show timing, not cause", output)

    def test_left_right_cycle_the_chart(self) -> None:
        app = self._app()
        app._handle_key("right")
        self.assertEqual(app.timeline_metric, "handles")
        self.assertIn("TIMELINE  HANDLES", render_text(app))
        app._handle_key("left")
        app._handle_key("left")
        self.assertEqual(app.timeline_metric, "cpu_percent")

    def test_markers_appear_on_the_chart(self) -> None:
        app = self._app()
        app._handle_key("m")
        marker = app.markers[-1]
        # Place the marker inside the charted range, as a live session would.
        app._timeline[-1] = replace(marker, timestamp=1005.0)
        output = render_text(app)
        self.assertIn("1 marker", output)
        self.assertIn("marker 1", output)

    def test_freeze_holds_the_view_while_sampling_continues(self) -> None:
        app = self._app()
        app._handle_key("f")
        self.assertTrue(app.timeline_frozen)
        before = render_text(app)
        self.assertIn("FROZEN at", before)
        self.assertIn("0 new samples", before)
        growing_session_more(app)
        output = render_text(app)
        self.assertIn("5 new samples", output)
        self.assertIn("25 samples over 12s", output)  # the frozen range, not the new one
        self.assertIn("TIMELINE FROZEN", output.splitlines()[3])  # the status line
        self.assertFalse(app.detector.is_paused)
        app._handle_key("1")
        self.assertIn("TIMELINE FROZEN", render_text(app).splitlines()[0])
        app._handle_key("4")
        app._handle_key("F")
        self.assertFalse(app.timeline_frozen)
        self.assertIn("30 samples", render_text(app))

    def test_frozen_caption_distinguishes_new_samples_from_retention_loss(self) -> None:
        app = _app()
        sample_trees(app, [[make_process(100)]] * 3)
        app._toggle_freeze()
        sample_trees(app, [[make_process(100)]] * 2, start=3)
        text = app._decimation_text(3, 1, 1).plain
        self.assertIn("2 newer samples collected while frozen", text)
        self.assertNotIn("earlier samples", text)

    def test_retention_caption_uses_the_sample_count_at_freeze(self) -> None:
        class Limited(DashboardApp):
            HISTORY_LIMIT = 2

        app = Limited("worker.exe", console=make_console())
        sample_trees(app, [[make_process(100)]] * 3)
        app._toggle_freeze()
        sample_trees(app, [[make_process(100)]] * 2, start=3)
        text = app._decimation_text(2, 1, 1).plain
        self.assertIn("2 newer samples collected while frozen", text)
        self.assertIn("earlier samples are not charted here", text)
        self.assertIn("report charts may be thinned; session facts remain exact", text)
        self.assertNotIn("keep the whole session", text)
        app._toggle_freeze()
        text = app._decimation_text(2, 1, 1).plain
        self.assertNotIn("while frozen", text)
        self.assertIn("earlier samples are not charted here", text)

    def test_paused_samples_leave_a_gap_and_a_mark(self) -> None:
        from helpers import make_snapshot

        app = _app(120, 40)
        for index in range(10):
            app._accept_snapshot(make_snapshot(timestamp=1000.0 + index * 0.5))
        app._accept_snapshot(make_snapshot(timestamp=1005.0, paused=True))
        for index in range(10):
            app._accept_snapshot(make_snapshot(timestamp=1030.0 + index * 0.5))
        app._handle_key("4")
        output = render_text(app)
        self.assertIn("P pause", output)
        self.assertIn("> resume", output)

    def test_before_enough_samples(self) -> None:
        app = _app(80, 24)
        app._handle_key("4")
        output = render_text(app)
        self.assertIn("Collecting samples for the timeline", output)
        self.assert_fills(output, 80, 24)


def growing_session_more(app: DashboardApp, samples: int = 5) -> None:
    root = replace(make_process(100, ppid=1, name="parent.exe"), create_time=1.0)
    trees = [[root, _worker(2, 200 + index)] for index in range(samples)]
    sample_trees(app, trees, start=100)


class MonitorViewTests(ViewRenderingMixin, unittest.TestCase):
    VIEW_KEY = "5"

    def test_shows_cadence_cost_coverage_and_host_context(self) -> None:
        output = " ".join(self.rendered(120, 40).split())
        self.assertIn("MONITOR DIAGNOSTICS", output)
        self.assertIn("requested 500 ms", output)
        self.assertIn("actual 500 ms (+0 ms)", output)
        self.assertIn("PID 7 access denied", output)
        self.assertIn("1 process unreadable (PID 7)", output)
        self.assertIn("handle types unavailable", output)
        self.assertIn("HOST CONTEXT", output)
        self.assertIn("not target usage or", output)
        self.assertIn("25 collected", output)

    def test_before_any_sample(self) -> None:
        app = _app(80, 24)
        app._handle_key("5")
        output = render_text(app)
        self.assert_fills(output, 80, 24)
        self.assertIn("none yet", output)

    def test_host_values_match_report_and_remain_reachable_in_narrow_terminals(self) -> None:
        from unittest import mock

        from helpers import make_snapshot

        from src.events import HostPressure, MonitorDiagnostics
        from src.html_report import render_html_report

        app = _app(80, 24, LEGACY)
        host = HostPressure(1000, age_seconds=2, cpu_percent=0,
                            available_memory_bytes=0, total_memory_bytes=MB, collection_ms=0.25)
        app._accept_snapshot(replace(make_snapshot(timestamp=1002),
                                     diagnostics=MonitorDiagnostics(500, host=host)))
        app._handle_key("5")
        app._handle_key("end")
        with mock.patch("src.view_monitor.time.time", return_value=1002):
            text = " ".join(render_text(app).split())
        self.assertIn("0.0% of host capacity", text)
        self.assertIn("0.0 B available physical", text)
        self.assertIn("2.0s old; 0.25 ms", text)
        report = app.build_report()
        self.assertEqual(report["session_facts"]["monitor"]["host_context"]["age_seconds"], 2)
        document = render_html_report(report)
        self.assertIn("0.0% of host capacity", document)
        self.assertIn("2.0s old at final sample", document)
        self.assertIn("0.25 ms pass", document)
        app._diagnostics = MonitorDiagnostics(500, host=HostPressure(1000))
        rows = dict(app._monitor_host_rows())
        self.assertEqual(rows["CPU"], "n/a")
        self.assertEqual(rows["Memory"], "n/a")


if __name__ == "__main__":
    unittest.main()
