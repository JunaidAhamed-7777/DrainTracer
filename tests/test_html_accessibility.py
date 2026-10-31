"""Offline report accessibility contracts and light/dark text contrast."""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser

from test_html_report import _report

from src.html_report import render_html_report
from src.resources import resource_path


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055)**2.4
              for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))


def _contrast(first: str, second: str) -> float:
    dark, light = sorted((_luminance(first), _luminance(second)))
    return (light + .05) / (dark + .05)


class _Semantics(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))


class ReportAccessibilityTests(unittest.TestCase):
    def test_text_badges_and_tooltips_meet_aa_contrast_in_both_themes(self) -> None:
        css = resource_path("leak_reports/frames/report.css").read_text(encoding="utf-8")
        dark, light = css.split("@media (prefers-color-scheme:light)", 1)
        for theme, source in (("dark", dark), ("light", light.split("*{box-sizing", 1)[0])):
            tokens = dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-f]{6})", source))
            pairs = [("text", "bg"), ("muted", "surface"), ("muted", "surface-2")]
            pairs += [("on-badge", tier) for tier in ("ok", "low", "medium", "high", "critical")]
            for foreground, background in pairs:
                with self.subTest(theme=theme, foreground=foreground, background=background):
                    self.assertGreaterEqual(_contrast(tokens[foreground], tokens[background]), 4.5)

    def test_native_controls_and_tables_have_accessible_names_and_headers(self) -> None:
        parser = _Semantics()
        parser.feed(render_html_report(_report()))
        labels = {attributes.get("for") for tag, attributes in parser.elements if tag == "label"}
        for tag, attributes in parser.elements:
            if tag == "input":
                self.assertIn(attributes.get("id"), labels)
                self.assertEqual(attributes.get("aria-describedby"), "range-status")
            elif tag == "th":
                self.assertEqual(attributes.get("scope"), "col")
            elif attributes.get("class") == "table-scroll":
                self.assertEqual(attributes.get("tabindex"), "0")
                self.assertTrue(attributes.get("aria-label"))
        self.assertTrue(any(tag == "summary" for tag, _attributes in parser.elements))

    def test_static_values_do_not_depend_on_executing_chart_javascript(self) -> None:
        document = render_html_report(_report())
        static = document.split('<script type="application/json"', 1)[0]
        self.assertIn("HIGH leak signal observed", static)
        self.assertIn("Private bytes (bytes)", static)
        self.assertIn("10,485,760", static)
        self.assertIn("worker.exe", static)
        self.assertIn("Download retained samples as CSV", static)


if __name__ == "__main__":
    unittest.main()
