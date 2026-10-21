"""Self-contained HTML leak reports with interactive trend charts.

The page is one file with no network access: styles, the chart script,
and the data are inlined, and a Content-Security-Policy that pins the
inline style and script by hash blocks everything else. Static sections
are rendered here with every value escaped; the script draws the charts
from a JSON payload and only ever inserts text with ``textContent``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .analysis import (
    RESOURCES,
    ReportSummary,
    chart_payload,
    finite,
    load_samples,
    summarize,
)
from .handle_types import describe_type_growth
from .report import report_filename, write_text_atomic
from .units import format_bytes, format_clock, format_duration

_RESOURCE_TITLES = {
    "memory": "Memory · private bytes",
    "handles": "Handles",
    "threads": "Threads",
}
_LEVELS = {"ok", "info", "warn", "alert", "error"}


def render_html_report(report: Mapping[str, Any]) -> str:
    """Return the complete HTML document for an exported report."""
    style = _load_frame("report.css")
    script = _load_frame("report.js")
    samples = load_samples(report)
    summary = summarize(report, samples)
    payload = chart_payload(report, samples, summary)
    start = payload["start"]

    target = str(report.get("target_path") or "unknown target")
    name = Path(target).name or target
    version = str(report.get("version") or "")
    title = f"DrainTracer · {name} · {summary.worst.upper() if summary.worst != 'none' else 'CLEAR'}"

    body = "\n".join(
        (
            _masthead(report, summary, name, target, version),
            '<main>',
            _verdict(report, summary),
            _cards(summary, _sample_count(report, summary)),
            _charts_panel(),
            _episodes_panel(summary, start),
            _events_panel(report, start),
            _processes_panel(report),
            '</main>',
            _footer(report, version),
        )
    )
    csp = (
        "default-src 'none'; "
        f"style-src '{_sha256(style)}'; "
        f"script-src '{_sha256(script)}'; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_e(csp)}">\n'
        '<meta name="color-scheme" content="dark light">\n'
        f"<title>{_e(title)}</title>\n"
        f"<style>{style}</style>\n</head>\n"
        f'<body>\n<div class="wrap">\n{body}\n</div>\n'
        f'<script type="application/json" id="drain-data">{_script_json(payload)}</script>\n'
        f"<script>{script}</script>\n</body>\n</html>\n"
    )


def write_html_report(report: Mapping[str, Any], directory: str | Path) -> Path:
    """Store the report as ``<directory>/leak_report_<report_id>.html``."""
    report_id = report.get("report_id")
    if not report_id:
        raise ValueError("report has no report_id")
    path = Path(directory) / report_filename(str(report_id), ".html")
    # Windows names may hold unpaired surrogates that UTF-8 cannot encode.
    return write_text_atomic(path, render_html_report(report), newline="\n", errors="replace")


# ----------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------


def _masthead(
    report: Mapping[str, Any], summary: ReportSummary, name: str, target: str, version: str
) -> str:
    mode = "Attached to a running process" if report.get("mode") == "attach" else "Launched by DrainTracer"
    interval = _first_interval(report)
    stride = _positive_int(report.get("snapshot_stride")) or 1
    facts: list[tuple[str, str]] = [
        ("Report ID", str(report.get("report_id") or "—")),
        ("Created", _local_time(report.get("created_at"))),
        ("Mode", mode),
        ("Root PID", str(report.get("root_pid") or "—")),
        ("Started", _local_time(report.get("session_started_at"))),
        ("Ended", _local_time(report.get("session_ended_at"), "still running at export")),
        ("Monitored", format_clock(summary.duration)),
        (
            "Samples",
            f"{_sample_count(report, summary):,}"
            + (f" every {interval:g} ms" if interval else "")
            + (f"; this report keeps 1 in {stride}" if stride > 1 else ""),
        ),
        ("Processes", f"up to {summary.max_processes}"),
    ]
    warmup = report.get("warmup")
    if isinstance(warmup, Mapping) and finite(warmup.get("seconds")):
        seconds = format_duration(finite(warmup.get("seconds")) or 0.0)
        facts.append(("Warm-up", seconds if warmup.get("completed") else f"{seconds}, did not finish"))
    exit_code = report.get("target_exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        facts.append(("Exit code", str(exit_code)))
    items = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in facts)
    return (
        '<header class="masthead">'
        f'<div class="brand"><span aria-hidden="true">&#9670;</span> DRAINTRACER'
        f'<span class="version">{_e(version)}</span><span class="kind">Leak report</span></div>'
        f"<h1>{_e(name)}</h1>"
        f'<p class="path">{_e(target)}</p>'
        f'<dl class="facts">{items}</dl>'
        "</header>"
    )


def _verdict(report: Mapping[str, Any], summary: ReportSummary) -> str:
    worst = summary.worst
    if worst == "none" and report.get("detection_ran") is False:
        # Older reports lack the field; only an explicit False is inconclusive.
        return (
            '<section class="verdict sev-low" aria-label="Verdict">'
            '<span class="badge sev-low">INCONCLUSIVE</span>'
            "<div><h2>No leak window was judged</h2>"
            f"<p>{_e(_inconclusive_reason(report))} A clean result here says nothing "
            "about leaks; monitor for longer.</p></div>"
            "</section>"
        )
    if worst == "none":
        headline = "No leak signals"
        detail = (
            "Memory, handles, and threads never showed sustained growth that met the "
            "detector's criteria. That is not proof the program is leak-free."
        )
    else:
        headline = f"{worst.upper()} leak signal observed"
        parts = []
        for resource in RESOURCES:
            stats = summary.resources[resource]
            if stats.worst == "none":
                parts.append(f"{resource} stayed clear")
            else:
                parts.append(f"{resource} reached {stats.worst.upper()}")
        detail = "; ".join(parts) + "."
    badge = "CLEAR" if worst == "none" else worst.upper()
    return (
        f'<section class="verdict sev-{_e(worst)}" aria-label="Verdict">'
        f'<span class="badge sev-{_e(worst)}">{_e(badge)}</span>'
        f"<div><h2>{_e(headline)}</h2><p>{_e(detail)}</p></div>"
        "</section>"
    )


def _cards(summary: ReportSummary, sample_count: int) -> str:
    cards = []
    for resource in RESOURCES:
        stats = summary.resources[resource]
        badge = "CLEAR" if stats.worst == "none" else stats.worst.upper()
        if stats.first is None or stats.last is None:
            big, line, drift = "—", "no measurements", ""
        else:
            big = _signed(resource, stats.growth or 0.0)
            line = (
                f"{_amount(resource, stats.first)} → {_amount(resource, stats.last)}"
                + (" since warm-up" if summary.warmup else "")
                + f" · peak {_amount(resource, stats.peak or 0.0)}"
            )
            drift = "drift " + _rate(resource, stats.drift_per_second or 0.0)
        episodes = f"{stats.episodes} signal episode{'s' if stats.episodes != 1 else ''}"
        types = _handle_type_line(summary) if resource == "handles" else ""
        cards.append(
            f'<article class="card"><h3>{_e(_RESOURCE_TITLES[resource])}'
            f'<span class="badge sev-{_e(stats.worst)}">{_e(badge)}</span></h3>'
            f'<div class="big">{_e(big)}</div>'
            f'<div class="sub">{_e(line)}</div>'
            f'<div class="sub">{_e(" · ".join(part for part in (drift, episodes) if part))}</div>'
            + (f'<div class="sub">{_e(types)}</div>' if types else "")
            + "</article>"
        )
    peaks = summary.peaks
    cards.append(
        '<article class="card"><h3>Session</h3>'
        f'<div class="big">{_e(format_clock(summary.duration))}</div>'
        f'<div class="sub">{_e(f"{sample_count:,} samples · up to {summary.max_processes} processes")}</div>'
        f'<div class="sub">{_e("peak RSS " + format_bytes(peaks.get("rss_bytes", 0.0)) + " · peak CPU " + format(peaks.get("cpu_percent", 0.0), ".1f") + "%")}</div>'
        "</article>"
    )
    return f'<section class="cards">{"".join(cards)}</section>'


def _charts_panel() -> str:
    return (
        '<section class="panel" id="trends" aria-label="Trends">'
        '<div class="panel-head"><h2>Trends</h2>'
        '<p class="hint">Hover for values · drag across a chart to zoom · '
        "double-click or Esc to reset · red dots mark where a leak signal was raised "
        "or escalated</p>"
        '<button type="button" class="reset" id="reset-zoom" hidden>Reset zoom</button></div>'
        '<div id="charts"><noscript><p class="empty">Charts need JavaScript; the tables '
        "below have the same findings.</p></noscript></div>"
        "</section>"
    )


def _episodes_panel(summary: ReportSummary, start: float | None) -> str:
    if not summary.episodes:
        body = '<p class="empty">No leak signals were raised during this session.</p>'
    else:
        rows = []
        for episode in summary.episodes:
            attributes = ""
            if start is not None:
                attributes = (
                    f' data-t="{episode.start - start:.3f}" data-t1="{episode.end - start:.3f}"'
                    ' title="Click to zoom the charts to this signal"'
                )
            rows.append(
                f"<tr{attributes}>"
                f"<td>{_e(episode.resource)}</td>"
                f'<td><span class="badge sev-{_e(episode.severity)}">{_e(episode.severity.upper())}</span></td>'
                f'<td class="num">{_e(_offset(episode.start, start))}</td>'
                f'<td class="num">{_e(format_clock(episode.duration))}</td>'
                f"<td>{_e(episode.message)}</td>"
                "</tr>"
            )
        body = _table(
            ("Resource", "Strongest", ("Started", "num"), ("Lasted", "num"), "Detail"), rows
        )
    return (
        '<section class="panel" aria-label="Leak signals">'
        '<div class="panel-head"><h2>Leak signals</h2>'
        '<p class="hint">One row per continuous signal; click a row to zoom the charts.</p></div>'
        f"{body}</section>"
    )


def _events_panel(report: Mapping[str, Any], start: float | None) -> str:
    rows = []
    structured = report.get("event_log")
    if isinstance(structured, (list, tuple)) and structured:
        for event in structured:
            if not isinstance(event, Mapping):
                continue
            stamp = finite(event.get("timestamp"))
            level = str(event.get("level", "info"))
            level = level if level in _LEVELS else "info"
            clock = (
                datetime.fromtimestamp(stamp).strftime("%H:%M:%S") if stamp is not None else ""
            )
            attributes = (
                f' data-t="{stamp - start:.3f}"' if stamp is not None and start is not None else ""
            )
            rows.append(
                f"<tr{attributes}>"
                f'<td class="num">{_e(clock)}</td>'
                f'<td class="num">{_e(_offset(stamp, start) if stamp is not None else "")}</td>'
                f'<td><span class="level level-{level}">{_e(level.upper())}</span></td>'
                f"<td>{_e(str(event.get('message', '')))}</td>"
                "</tr>"
            )
        body = _table((("Time", "num"), ("Elapsed", "num"), "Level", "Event"), rows)
    else:
        # Reports from before event_log only carry "HH:MM:SS message" strings.
        legacy = report.get("events")
        for line in legacy if isinstance(legacy, (list, tuple)) else ():
            clock, _, message = str(line).partition(" ")
            rows.append(f'<tr><td class="num">{_e(clock)}</td><td>{_e(message)}</td></tr>')
        body = _table((("Time", "num"), "Event"), rows)
    if not rows:
        body = '<p class="empty">No events were recorded.</p>'
    return (
        '<section class="panel" aria-label="Event log">'
        '<div class="panel-head"><h2>Event log</h2>'
        '<p class="hint">Hover a row to find it on the charts.</p></div>'
        f"{body}</section>"
    )


def _processes_panel(report: Mapping[str, Any]) -> str:
    snapshot = _last_snapshot_with_processes(report)
    if snapshot is None:
        body = '<p class="empty">No process measurements were recorded.</p>'
        caption = ""
    else:
        root_pid = snapshot.get("root_pid")
        types_by_pid = _process_handle_types(report)
        processes = [p for p in snapshot.get("processes", ()) if isinstance(p, Mapping)]
        processes.sort(key=lambda p: (p.get("pid") != root_pid, _int(p.get("pid"))))
        rows = []
        for process in processes:
            denied = bool(process.get("access_denied"))
            cells = [
                (str(process.get("pid", "")), "num"),
                (str(process.get("ppid") if process.get("ppid") is not None else "—"), "num"),
                (str(process.get("name", "")), ""),
                ("access denied" if denied else str(process.get("status", "")), ""),
            ]
            if denied:
                cells += [("n/a", "num")] * 5 + [("n/a", "")]
            else:
                cells += [
                    (f"{finite(process.get('cpu_percent')) or 0.0:.1f}%", "num"),
                    (format_bytes(finite(process.get("rss_bytes")) or 0.0), "num"),
                    (format_bytes(finite(process.get("private_bytes")) or 0.0), "num"),
                    (f"{_int(process.get('handles')):,}", "num"),
                    (f"{_int(process.get('threads')):,}", "num"),
                    (_type_breakdown(types_by_pid.get(process.get("pid"))) or "—", ""),
                ]
            row_class = ' class="root"' if process.get("pid") == root_pid else ""
            rows.append(
                f"<tr{row_class}>"
                + "".join(
                    f'<td class="{css}">{_e(text)}</td>' if css else f"<td>{_e(text)}</td>"
                    for text, css in cells
                )
                + "</tr>"
            )
        body = _table(
            (
                ("PID", "num"), ("Parent", "num"), "Name", "Status", ("CPU", "num"),
                ("RSS", "num"), ("Private", "num"), ("Handles", "num"), ("Threads", "num"),
                "Handle types",
            ),
            rows,
        )
        stamp = finite(snapshot.get("timestamp"))
        caption = (
            f"As of {datetime.fromtimestamp(stamp).strftime('%H:%M:%S')}, the last sample "
            "with live processes."
            if stamp is not None
            else ""
        )
    return (
        '<section class="panel" aria-label="Processes">'
        f'<div class="panel-head"><h2>Processes</h2><p class="hint">{_e(caption)}</p></div>'
        f"{body}</section>"
    )


def _footer(report: Mapping[str, Any], version: str) -> str:
    schema = report.get("report_schema")
    parts = [f"Generated by DrainTracer {version}".strip()]
    if schema is not None:
        parts.append(f"report schema {schema}")
    note = (
        "Leak signals are heuristics: they flag sustained growth worth investigating, "
        "not proof that memory, handles, or threads are never released."
    )
    return f"<footer><p>{_e(' · '.join(parts))}</p><p>{_e(note)}</p></footer>"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _sha256(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _script_json(payload: Mapping[str, Any]) -> str:
    """Serialize data for a script element without letting it end the element."""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _table(headers: Sequence[str | tuple[str, str]], rows: Sequence[str]) -> str:
    cells = []
    for header in headers:
        label, css = header if isinstance(header, tuple) else (header, "")
        cells.append(f'<th class="{css}">{_e(label)}</th>' if css else f"<th>{_e(label)}</th>")
    return (
        f'<div class="table-scroll"><table><thead><tr>{"".join(cells)}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _local_time(value: Any, missing: str = "—") -> str:
    if not value:
        return missing
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    offset = moment.strftime("%z")
    offset = f" {offset[:3]}:{offset[3:]}" if offset else ""
    return moment.strftime("%Y-%m-%d %H:%M:%S") + offset


def _offset(stamp: float | None, start: float | None) -> str:
    if stamp is None or start is None:
        return ""
    return "+" + format_clock(stamp - start)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _sample_count(report: Mapping[str, Any], summary: ReportSummary) -> int:
    """Samples the session took; a thinned report holds fewer snapshots."""
    recorded = _positive_int(report.get("sample_count"))
    return max(recorded or 0, summary.sample_count)


def _first_interval(report: Mapping[str, Any]) -> float | None:
    snapshots = report.get("snapshots")
    if isinstance(snapshots, (list, tuple)):
        for snapshot in snapshots:
            if isinstance(snapshot, Mapping):
                interval = finite(snapshot.get("sample_interval_ms"))
                if interval:
                    return interval
    return None


def _last_snapshot_with_processes(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    snapshots = report.get("snapshots")
    if not isinstance(snapshots, (list, tuple)):
        return None
    for snapshot in reversed(snapshots):
        if not isinstance(snapshot, Mapping):
            continue
        processes = snapshot.get("processes")
        if isinstance(processes, (list, tuple)) and processes:
            return snapshot
    return None


def _inconclusive_reason(report: Mapping[str, Any]) -> str:
    warmup = report.get("warmup")
    if isinstance(warmup, Mapping) and warmup.get("seconds") and not warmup.get("completed"):
        seconds = format_duration(finite(warmup.get("seconds")) or 0.0)
        return f"The {seconds} warm-up never finished, so leak detection did not run."
    settings = report.get("settings")
    windows = settings.get("windows_seconds") if isinstance(settings, Mapping) else None
    shortest = finite(windows[0]) if isinstance(windows, (list, tuple)) and windows else None
    if shortest:
        return f"Monitoring ended before the {format_duration(shortest)} window filled."
    return "Monitoring ended before any detection window filled."


def _type_breakdown(counts: Mapping[str, int] | None, limit: int = 3) -> str:
    """Largest named handle categories first: ``file 120 · event 80 · registry 12``."""
    if not counts:
        return ""
    ranked = sorted(
        ((count, category) for category, count in counts.items() if count > 0),
        key=lambda item: (item[1] == "other", -item[0]),
    )
    return " · ".join(f"{category} {count:,}" for count, category in ranked[:limit])


def _handle_type_line(summary: ReportSummary) -> str:
    """Which handle types grew over the run, or what the handles were at the end."""
    growth = describe_type_growth(summary.handle_type_growth or {})
    if growth:
        return growth
    breakdown = _type_breakdown(summary.handle_types)
    return f"at the end: {breakdown}" if breakdown else ""


def _process_handle_types(report: Mapping[str, Any]) -> dict[Any, Mapping[str, int]]:
    """Per-PID handle types from the last snapshot where they were classified."""
    snapshots = report.get("snapshots")
    if not isinstance(snapshots, (list, tuple)):
        return {}
    for snapshot in reversed(snapshots):
        if not isinstance(snapshot, Mapping) or not snapshot.get("handle_types"):
            continue
        found = {
            process.get("pid"): process["handle_types"]
            for process in snapshot.get("processes") or ()
            if isinstance(process, Mapping) and isinstance(process.get("handle_types"), Mapping)
        }
        if found:
            return found
    return {}


def _int(value: Any) -> int:
    number = finite(value)
    return int(number) if number is not None else 0


def _amount(resource: str, value: float) -> str:
    return format_bytes(value) if resource == "memory" else f"{value:,.0f}"


def _signed(resource: str, value: float) -> str:
    sign = "-" if value < 0 else "+"
    return sign + _amount(resource, abs(value))


def _rate(resource: str, per_second: float) -> str:
    """Growth rate per second, or per minute when that reads better."""
    sign = "-" if per_second < 0 else "+"
    magnitude = abs(per_second)
    small = magnitude < (1024 if resource == "memory" else 1.0)
    if small:
        magnitude *= 60
    unit = "/min" if small else "/s"
    if resource == "memory":
        return f"{sign}{format_bytes(magnitude)}{unit}"
    return f"{sign}{magnitude:,.2f}{unit}"


_FRAME_DIR = Path(__file__).resolve().parents[1] / "leak_reports" / "frames"


@lru_cache(maxsize=2)
def _load_frame(filename: str) -> str:
    """Load a shared asset at export time so missing HTML assets do not lose JSON."""
    return (_FRAME_DIR / filename).read_text(encoding="utf-8")


def __getattr__(name: str) -> str:
    """Preserve the former private asset imports for report-tool consumers."""
    assets = {"_STYLE": "report.css", "_SCRIPT": "report.js"}
    if name not in assets:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return _load_frame(assets[name])



__all__ = ["render_html_report", "write_html_report"]
