# Offline HTML accessibility verification

The report offers equivalent static evidence alongside its charts. Read
verdict/coverage cards, signal rows, process evidence, retained main-chart
samples, and separately sampled handle classifications without hovering or
dragging. Native disclosures expose tables; CSV downloads use embedded data
and work offline. Numeric range fields and signal Zoom buttons provide a
keyboard alternative to dragging. Column headers, named scroll regions,
control labels, visible focus, and text verdicts preserve meaning without
color alone. Inputs own validation text rather than browser pop-up errors.

## Reproduce the fixtures and checks

From a checkout with runtime dependencies installed:

```powershell
python tests/verify_html_accessibility.py C:\temp\draintracer-html-accessibility
python -m unittest discover -s tests -p test_html_accessibility.py
python -m unittest discover -s tests -p test_html_report.py
python -m unittest discover -s tests -p test_analysis.py
node --check leak_reports/frames/report.js
```

The generator writes synthetic `full`, `partial`, `unavailable`, `warmup`,
and `thinned` JSON/HTML pairs with the current application version. It
launches no process and makes no network requests. Keep generated files
outside the source tree. The full fixture contains 40 retained samples,
one HIGH signal at +20 seconds, and handle classifications every 5 seconds.
The thinned fixture preserves exact peaks/episodes while omitting chart
detail; it exercises the retained-data qualification explicitly.

The parent accessibility change depends on the focused coverage, unknown
process-counter, and thinned-signal fixes (#14, #15, #19), plus the retained
sample table (#62). Apply those fixes before judging their combined output.

## Recorded checks — 30 October 2026

Chromium's browser accessibility API and Windows accessibility tree exposed
the full fixture's named range controls, status announcements, native
sample disclosure, CSV links, semantic column headers, process table,
and native signal Zoom button. Applying 5–10 seconds changed the chart range;
reversed endpoints produced linked validation text and focused the invalid
field. The disclosure's assistive-technology Expand action exposed both
main and handle-category tables, and keyboard focus reached the named
horizontal table region. Every visible enabled control had a nonnegative
tab index. The browser reported no console errors for the exercised flow.

A 375 CSS-pixel viewport retained the controls and confined wide tables to
their horizontal scroll regions: the document's scroll width stayed within
the viewport. Desktop table inspection showed readable units, aligned
numeric cells, and a visible focus outline. The temporary viewport override
was reset afterward. Native browser keyboard automation was interrupted by
user window activity; the disclosure's assistive action and native semantics
were checked instead of claiming an unobserved key activation.

Automated contrast checks use the actual CSS tokens in both themes. Body,
tooltip secondary text, and every severity badge meet 4.5:1; the tested
minimum ratios are approximately 4.69:1 (dark) and 4.94:1 (light). Range
fields/buttons use visible borders and focus outlines. Reduced-motion
preference disables smooth scrolling to a signal; forced-colors mode keeps
system scrollbars, focus outlines, and badge boundaries.

These checks cover the browser accessibility interfaces and the documented
fixtures. They are not a claim of certification for every screen-reader
client, browser version, operating-system theme, or custom font.
