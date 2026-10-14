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
            _cards(summary),
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
        f"style-src '{_sha256(_STYLE)}'; "
        f"script-src '{_sha256(_SCRIPT)}'; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_e(csp)}">\n'
        '<meta name="color-scheme" content="dark light">\n'
        f"<title>{_e(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n"
        f'<body>\n<div class="wrap">\n{body}\n</div>\n'
        f'<script type="application/json" id="drain-data">{_script_json(payload)}</script>\n'
        f"<script>{_SCRIPT}</script>\n</body>\n</html>\n"
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
            f"{summary.sample_count:,}" + (f" every {interval:g} ms" if interval else ""),
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


def _cards(summary: ReportSummary) -> str:
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
        f'<div class="sub">{_e(f"{summary.sample_count:,} samples · up to {summary.max_processes} processes")}</div>'
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


_STYLE = """
:root{color-scheme:dark;--bg:#16161e;--surface:#1f2335;--surface-2:#292e42;--border:#3b4261;
--grid:#2a2f45;--text:#c0caf5;--muted:#8089b3;--accent:#7aa2f7;--accent-alt:#bb9af7;
--cyan:#7dcfff;--teal:#73daca;--ok:#9ece6a;--low:#e0af68;--medium:#ff9e64;--high:#f7768e;
--critical:#ff4f6d;--on-badge:#16161e;--paused:rgba(128,137,179,.16);--marker:#ff4f6d}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#f4f5fa;--surface:#ffffff;
--surface-2:#eef0f7;--border:#d4d8e8;--grid:#e8ebf3;--text:#1f2335;--muted:#5d6488;
--accent:#2e5bd8;--accent-alt:#8a4fd6;--cyan:#0b7fa6;--teal:#138a78;--ok:#3d8b2a;
--low:#a8700b;--medium:#c45a10;--high:#d0324f;--critical:#a3001f;--on-badge:#ffffff;
--paused:rgba(93,100,136,.12);--marker:#d0324f}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 "Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 40px}
.brand{display:flex;flex-wrap:wrap;align-items:center;gap:8px;color:var(--accent);
font-size:12px;font-weight:700;letter-spacing:.14em}
.brand .version{color:var(--muted);letter-spacing:.02em}
.brand .kind{color:var(--muted);font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.brand .kind::before{content:"/";margin-right:8px;color:var(--border)}
h1{margin:6px 0 2px;font-size:28px;line-height:1.2;word-break:break-word}
h2{margin:0;font-size:16px}
.path{margin:0;color:var(--muted);word-break:break-all;
font:12px/1.4 ui-monospace,"Cascadia Code",Consolas,monospace}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px 18px;
margin:18px 0 0;padding:0}
.facts div{min-width:0}
.facts dt{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.facts dd{margin:2px 0 0;font-variant-numeric:tabular-nums;word-break:break-word}
.sev-none{--sev:var(--ok)}.sev-low{--sev:var(--low)}.sev-medium{--sev:var(--medium)}
.sev-high{--sev:var(--high)}.sev-critical{--sev:var(--critical)}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--sev);
color:var(--on-badge);font-size:11px;font-weight:700;letter-spacing:.06em;white-space:nowrap}
.verdict{display:flex;align-items:center;gap:16px;margin:24px 0 16px;padding:16px 18px;
border:1px solid var(--border);border-left:4px solid var(--sev);border-radius:12px;
background:var(--surface)}
.verdict .badge{font-size:13px;padding:5px 12px}
.verdict p{margin:2px 0 0;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;
margin-bottom:16px}
.card{min-width:0;padding:14px 16px;border:1px solid var(--border);border-radius:12px;
background:var(--surface)}
.card h3{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0;
color:var(--muted);font-size:11.5px;letter-spacing:.08em;text-transform:uppercase}
.card .big{margin:6px 0 2px;font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.card .sub{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.panel{margin-bottom:16px;padding:16px 18px;border:1px solid var(--border);border-radius:12px;
background:var(--surface)}
.panel-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
gap:6px 16px;margin-bottom:8px}
.hint{margin:0;color:var(--muted);font-size:12.5px}
.reset{padding:4px 12px;border:1px solid var(--border);border-radius:8px;
background:var(--surface-2);color:var(--text);font:inherit;cursor:pointer}
.reset:hover,.reset:focus-visible{border-color:var(--accent)}
.chart{padding-top:10px;border-top:1px solid var(--grid)}
.chart:first-child{border-top:0;padding-top:0}
.chart-head{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin:4px 0}
.chart-head h3{margin:0;font-size:12px;letter-spacing:.08em;text-transform:uppercase}
.legend{display:flex;flex-wrap:wrap;gap:4px}
.legend-item{display:inline-flex;align-items:center;gap:6px;padding:1px 8px;
border:1px solid transparent;border-radius:6px;background:none;color:var(--text);
font:inherit;font-size:12.5px;cursor:pointer}
.legend-item:hover,.legend-item:focus-visible{border-color:var(--border)}
.legend-item[aria-pressed="false"]{opacity:.45;text-decoration:line-through}
.swatch{display:inline-block;width:14px;height:3px;border-radius:2px;background:var(--series)}
.latest{margin-left:auto;color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.plot{position:relative}
.chart-svg{display:block;width:100%;cursor:crosshair;touch-action:pan-y;user-select:none;
-webkit-user-select:none}
.s-private{--series:var(--accent)}.s-rss{--series:var(--cyan)}.s-handles{--series:var(--low)}
.s-threads{--series:var(--teal)}.s-cpu{--series:var(--accent-alt)}
.h-file{--series:var(--low)}.h-socket{--series:var(--critical)}.h-registry{--series:var(--teal)}
.h-event{--series:var(--accent-alt)}.h-thread{--series:var(--cyan)}.h-process{--series:var(--accent)}
.h-mutex{--series:var(--medium)}.h-semaphore{--series:var(--ok)}.h-section{--series:var(--text)}
.h-other{--series:var(--muted)}
.line{fill:none;stroke:var(--series);stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round}
.line.s-rss{stroke-width:1.4;stroke-dasharray:5 4;opacity:.85}
.line.h-other{stroke-width:1.4;stroke-dasharray:2 3}
.grid line{stroke:var(--grid)}
.grid .tick,.axis{stroke:var(--border)}
.axis-label{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.axis-label.y{text-anchor:end;dominant-baseline:middle}
.axis-label.x{text-anchor:middle}
.paused{fill:var(--paused)}
.warmup{fill:var(--accent);fill-opacity:.08}
.warmup-label{fill:var(--muted);font-size:11px}
.span{fill:var(--sev);opacity:.14}
.exit{stroke:var(--muted);stroke-dasharray:3 4}
.marker{fill:var(--marker);stroke:var(--surface);stroke-width:2}
.cross line{stroke:var(--muted);stroke-dasharray:2 3}
.dot{fill:var(--series);stroke:var(--surface);stroke-width:1.5}
.brush{fill:var(--accent);fill-opacity:.15;stroke:var(--accent)}
.no-data{fill:var(--muted);font-size:12px;text-anchor:middle}
.tip{position:absolute;top:6px;z-index:2;min-width:190px;max-width:min(360px,80%);padding:8px 10px;
border:1px solid var(--border);border-radius:8px;background:var(--surface-2);
box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:12.5px;pointer-events:none}
.tip-time{margin-bottom:4px;color:var(--muted);font-variant-numeric:tabular-nums}
.tip-row{display:flex;align-items:center;gap:8px}
.tip-label{flex:1}
.tip-value{font-weight:600;font-variant-numeric:tabular-nums}
.tip-signal{margin-top:6px;color:var(--sev);font-weight:700}
.tip-message{margin-top:2px;color:var(--muted)}
.table-scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
th{color:var(--muted);font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.num{text-align:right;white-space:nowrap}
tr[data-t]:hover,tr[data-t]:focus-visible{background:var(--surface-2);outline:none}
tr[data-t1]{cursor:zoom-in}
tr.root td:nth-child(3){font-weight:700}
.level{font-size:11px;font-weight:700;letter-spacing:.06em}
.level-ok{color:var(--ok)}.level-info{color:var(--accent)}.level-warn{color:var(--low)}
.level-alert{color:var(--medium)}.level-error{color:var(--high)}
.empty{margin:6px 0;color:var(--muted)}
footer{margin-top:24px;color:var(--muted);font-size:12px;text-align:center}
footer p{margin:2px 0}
@media (max-width:640px){.wrap{padding:18px 12px 32px}h1{font-size:22px}
.facts{grid-template-columns:1fr 1fr;gap:8px 12px}
.latest{width:100%;margin-left:0}.verdict{align-items:flex-start}}
@media print{body{background:#fff;color:#000}.reset,.hint{display:none}
.panel,.card,.verdict{break-inside:avoid}}
"""

_SCRIPT = """
(() => {
  "use strict";
  const host = document.getElementById("charts");
  if (!host) return;
  let data = null;
  try {
    data = JSON.parse(document.getElementById("drain-data").textContent);
  } catch (error) {
    data = null;
  }
  const SVG = "http://www.w3.org/2000/svg";

  function html(tag, className, text, parent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null && text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function svgNode(tag, attributes, parent) {
    const node = document.createElementNS(SVG, tag);
    for (const name of Object.keys(attributes)) node.setAttribute(name, String(attributes[name]));
    if (parent) parent.appendChild(node);
    return node;
  }

  if (!data || !Array.isArray(data.t) || data.t.length === 0) {
    html("p", "empty", "No samples were recorded for this session.", host);
    return;
  }

  // A timeline is one time base and the series sampled on it. Handle types
  // are classified every few seconds, so they have their own.
  function timeline(times, values) {
    const firstAtOrAfter = (t) => {
      let low = 0;
      let high = times.length;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (times[middle] < t) low = middle + 1; else high = middle;
      }
      return low;
    };
    const nearestIndex = (t) => {
      const index = firstAtOrAfter(t);
      if (index <= 0) return 0;
      if (index >= times.length) return times.length - 1;
      return t - times[index - 1] <= times[index] - t ? index - 1 : index;
    };
    return { times, values, firstAtOrAfter, nearestIndex };
  }
  const main = timeline(data.t, data.series || {});
  const typeData = data.handle_types || {};
  const types = timeline(Array.isArray(typeData.t) ? typeData.t : [], typeData.series || {});
  const T = main.times;
  const full = [T[0], T[T.length - 1] > T[0] ? T[T.length - 1] : T[0] + 1];
  const step = T.length > 1 ? (T[T.length - 1] - T[0]) / (T.length - 1) : 1;
  let view = full.slice();
  const hidden = new Set();
  const resetButton = document.getElementById("reset-zoom");

  const CHARTS = [
    // minSpan keeps small noise on a large baseline from filling the chart.
    { key: "memory", title: "Memory", unit: "bytes", resource: "memory", line: main,
      minSpan: (top) => Math.max(1048576, top * 0.02), series: [
      { key: "private", label: "Private bytes", cls: "s-private" },
      { key: "rss", label: "Working set (RSS)", cls: "s-rss" } ] },
    { key: "handles", title: "Handles", unit: "count", resource: "handles", line: main,
      minSpan: (top) => Math.max(10, top * 0.05), series: [
      { key: "handles", label: "Handles", cls: "s-handles" } ] },
    { key: "threads", title: "Threads", unit: "count", resource: "threads", line: main,
      minSpan: (top) => Math.max(4, top * 0.05), series: [
      { key: "threads", label: "Threads", cls: "s-threads" } ] },
    { key: "cpu", title: "CPU", unit: "percent", resource: null, line: main,
      minSpan: () => 10, series: [
      { key: "cpu", label: "CPU, sum of processes", cls: "s-cpu" } ] },
  ];
  const HANDLE_LABELS = { file: "Files", socket: "Sockets", registry: "Registry keys",
    event: "Events", thread: "Threads", process: "Processes", mutex: "Mutexes",
    semaphore: "Semaphores", section: "Sections", other: "Other" };
  const known = (category) => Object.prototype.hasOwnProperty.call(HANDLE_LABELS, category);
  const categories = Array.isArray(typeData.categories) ? typeData.categories : [];
  if (types.times.length > 0 && categories.length > 0) {
    // Handle signals shade this chart too; markers stay on the total.
    CHARTS.splice(2, 0, { key: "types", title: "Handle types", unit: "count",
      resource: "handles", markers: false, line: types,
      minSpan: (top) => Math.max(10, top * 0.05),
      series: categories.map((category) => ({ key: category,
        label: known(category) ? HANDLE_LABELS[category] : category,
        cls: "h-" + (known(category) ? category : "other") })) });
  }

  const pad2 = (value) => String(value).padStart(2, "0");
  const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];
  function formatBytes(value) {
    let amount = Math.abs(value);
    let index = 0;
    while (amount >= 1024 && index < BYTE_UNITS.length - 1) { amount /= 1024; index += 1; }
    const digits = index === 0 || amount >= 100 ? 0 : 1;
    return (value < 0 ? "-" : "") + amount.toFixed(digits) + " " + BYTE_UNITS[index];
  }
  const FORMAT = {
    bytes: formatBytes,
    count: (value) => Math.round(value).toLocaleString("en-US"),
    percent: (value) => value.toFixed(1) + "%",
  };
  function elapsed(seconds, precise) {
    const sign = seconds < 0 ? "-" : "";
    let rest = Math.abs(seconds);
    if (precise && rest < 60) return sign + rest.toFixed(1) + "s";
    rest = Math.round(rest);
    const hours = Math.floor(rest / 3600);
    const minutes = Math.floor((rest % 3600) / 60);
    const secs = rest % 60;
    return sign + (hours ? hours + ":" + pad2(minutes) : String(minutes)) + ":" + pad2(secs);
  }
  function clock(seconds) {
    if (typeof data.start !== "number") return "";
    const date = new Date((data.start + seconds) * 1000);
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  }

  function valueNear(values, index) {
    for (let offset = 0; offset <= 3; offset += 1) {
      for (const candidate of [index - offset, index + offset]) {
        if (candidate >= 0 && candidate < values.length && values[candidate] != null) {
          return values[candidate];
        }
      }
    }
    return null;
  }
  const isHidden = (chart, series) => hidden.has(chart.key + "/" + series.key);

  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    const normalized = raw / magnitude;
    return (normalized < 1.5 ? 1 : normalized < 3 ? 2 : normalized < 7 ? 5 : 10) * magnitude;
  }
  function yScale(chart, first, last) {
    let low = Infinity;
    let high = -Infinity;
    for (const series of chart.series) {
      if (isHidden(chart, series)) continue;
      const values = chart.line.values[series.key] || [];
      for (let index = first; index < last; index += 1) {
        const value = values[index];
        if (value == null) continue;
        if (value < low) low = value;
        if (value > high) high = value;
      }
    }
    if (low === Infinity) return null;
    if (chart.unit === "percent") low = 0;
    const floor = chart.minSpan(Math.max(Math.abs(low), Math.abs(high)));
    if (high - low < floor) {
      const extra = floor - (high - low);
      const nonNegative = low >= 0;
      low -= extra / 2;
      high += extra / 2;
      if (nonNegative && low < 0) { high -= low; low = 0; }
    }
    let scale = 1;
    let suffix = "";
    if (chart.unit === "bytes") {
      let top = Math.max(Math.abs(low), Math.abs(high));
      let index = 0;
      while (top >= 1024 && index < BYTE_UNITS.length - 1) { top /= 1024; index += 1; }
      scale = Math.pow(1024, index);
      suffix = " " + BYTE_UNITS[index];
    }
    let a = low / scale;
    let b = high / scale;
    if (b - a < 1e-9) {
      const pad = Math.max(Math.abs(b) * 0.05, 1);
      a -= pad;
      b += pad;
    }
    let tickStep = niceStep(b - a, 4);
    if (chart.unit === "count") tickStep = Math.max(1, tickStep);
    a = Math.floor(a / tickStep) * tickStep;
    b = Math.ceil(b / tickStep) * tickStep;
    if (low >= 0 && a < 0) a = 0;
    const digits = tickStep < 1 ? Math.min(3, Math.ceil(-Math.log10(tickStep))) : 0;
    const ticks = [];
    for (let value = a; value <= b + tickStep / 2; value += tickStep) ticks.push(value * scale);
    const label = (value) => {
      const shown = (value / scale).toFixed(digits);
      if (chart.unit === "percent") return shown + "%";
      if (chart.unit === "bytes") return shown + suffix;
      return Number(shown).toLocaleString("en-US");
    };
    return { low: a * scale, high: b * scale, ticks, label };
  }

  const TIME_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400];
  function timeTicks(a, b, width) {
    const most = Math.max(2, Math.floor(width / 84));
    let chosen = TIME_STEPS[TIME_STEPS.length - 1];
    for (const candidate of TIME_STEPS) {
      if ((b - a) / candidate <= most) { chosen = candidate; break; }
    }
    const values = [];
    for (let value = Math.ceil(a / chosen) * chosen; value <= b + 1e-9; value += chosen) {
      values.push(Math.round(value * 1000) / 1000);
    }
    return { values, precise: chosen < 1 };
  }

  function linePath(times, values, first, last, x, y, pixels) {
    let path = "";
    let pen = false;
    const to = (index) => {
      path += (pen ? "L" : "M") + x(times[index]).toFixed(1) + "," + y(values[index]).toFixed(1);
      pen = true;
    };
    const count = last - first;
    if (count <= pixels * 2) {
      for (let index = first; index < last; index += 1) {
        if (values[index] == null) { pen = false; continue; }
        to(index);
      }
      return path;
    }
    // Keep each pixel column's minimum and maximum so spikes survive.
    const buckets = Math.max(1, Math.floor(pixels));
    for (let bucket = 0; bucket < buckets; bucket += 1) {
      const start = first + Math.floor((bucket * count) / buckets);
      const end = first + Math.floor(((bucket + 1) * count) / buckets);
      let low = -1;
      let high = -1;
      let gap = false;
      for (let index = start; index < end; index += 1) {
        const value = values[index];
        if (value == null) { gap = true; continue; }
        if (low < 0 || value < values[low]) low = index;
        if (high < 0 || value > values[high]) high = index;
      }
      if (low < 0) { pen = false; continue; }
      if (low === high) to(low);
      else if (low < high) { to(low); to(high); }
      else { to(high); to(low); }
      if (gap) pen = false;
    }
    return path;
  }

  function peakText(chart) {
    if (chart.key === "types") {
      const times = chart.line.times;
      const every = times.length > 1 ? (times[times.length - 1] - times[0]) / (times.length - 1) : 0;
      return every > 0 ? "classified about every " + every.toFixed(1) + " s" : "classified once";
    }
    const series = chart.series[0];
    const values = chart.line.values[series.key] || [];
    let peak = null;
    for (const value of values) if (value != null && (peak === null || value > peak)) peak = value;
    return peak === null ? "no measurements" : "peak " + FORMAT[chart.unit](peak);
  }

  const charts = CHARTS.map((def) => {
    const chart = { def, geom: null, cross: null, brush: null, markers: [] };
    const section = html("section", "chart", null, host);
    const head = html("div", "chart-head", null, section);
    html("h3", null, def.title, head);
    const legend = html("div", "legend", null, head);
    for (const series of def.series) {
      const button = html("button", "legend-item " + series.cls, null, legend);
      button.type = "button";
      button.setAttribute("aria-pressed", "true");
      button.title = "Show or hide this series";
      html("span", "swatch", null, button);
      html("span", null, series.label, button);
      button.addEventListener("click", () => {
        const id = def.key + "/" + series.key;
        if (hidden.has(id)) hidden.delete(id); else hidden.add(id);
        button.setAttribute("aria-pressed", String(!hidden.has(id)));
        draw(chart);
      });
    }
    html("div", "latest", peakText(def), head);
    chart.plot = html("div", "plot", null, section);
    chart.svg = svgNode("svg", { class: "chart-svg", role: "img",
      "aria-label": def.title + " over the session" }, chart.plot);
    chart.tip = html("div", "tip", null, chart.plot);
    chart.tip.hidden = true;
    return chart;
  });

  function draw(chart) {
    const def = chart.def;
    const svg = chart.svg;
    const width = Math.max(260, Math.floor(chart.plot.clientWidth || 0));
    const height = 200;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const m = { left: 66, right: 14, top: 12, bottom: 28 };
    const plotWidth = width - m.left - m.right;
    const plotHeight = height - m.top - m.bottom;
    const a = view[0];
    const b = view[1];
    const line = def.line;
    const first = Math.max(0, line.firstAtOrAfter(a) - 1);
    const last = Math.min(line.times.length, line.firstAtOrAfter(b) + 1);
    const scale = yScale(def, first, last);
    const x = (t) => m.left + ((t - a) / (b - a)) * plotWidth;
    const y = (value) => scale
      ? m.top + plotHeight - ((value - scale.low) / ((scale.high - scale.low) || 1)) * plotHeight
      : m.top + plotHeight;

    const clipId = "clip-" + def.key;
    const clip = svgNode("clipPath", { id: clipId }, svgNode("defs", {}, svg));
    svgNode("rect", { x: m.left, y: m.top, width: plotWidth, height: plotHeight }, clip);

    const grid = svgNode("g", { class: "grid" }, svg);
    if (scale) {
      for (const value of scale.ticks) {
        const yy = y(value);
        svgNode("line", { x1: m.left, x2: m.left + plotWidth, y1: yy, y2: yy }, grid);
        svgNode("text", { x: m.left - 8, y: yy, class: "axis-label y" }, grid).textContent =
          scale.label(value);
      }
    }
    const xTicks = timeTicks(a, b, plotWidth);
    for (const t of xTicks.values) {
      const xx = x(t);
      svgNode("line", { x1: xx, x2: xx, y1: m.top + plotHeight, y2: m.top + plotHeight + 4,
        class: "tick" }, grid);
      svgNode("text", { x: xx, y: m.top + plotHeight + 18, class: "axis-label x" }, grid)
        .textContent = elapsed(t, xTicks.precise);
    }

    const area = svgNode("g", { "clip-path": "url(#" + clipId + ")" }, svg);
    const warm = data.warmup;
    if (Array.isArray(warm) && warm[1] >= a && warm[0] <= b) {
      const left = x(Math.max(a, warm[0]));
      const right = x(Math.min(b, warm[1]));
      svgNode("rect", { x: left, y: m.top, width: Math.max(1, right - left), height: plotHeight,
        class: "warmup" }, area);
      if (right - left > 64) {
        svgNode("text", { x: left + 6, y: m.top + 14, class: "warmup-label" }, area)
          .textContent = "warm-up";
      }
    }
    for (const range of data.paused || []) {
      if (range[1] < a || range[0] > b) continue;
      svgNode("rect", { x: x(range[0]), y: m.top, width: Math.max(1, x(range[1]) - x(range[0])),
        height: plotHeight, class: "paused" }, area);
    }
    if (def.resource) {
      for (const span of data.spans || []) {
        if (span.resource !== def.resource || span.end < a || span.start > b) continue;
        const left = x(span.start);
        svgNode("rect", { x: left, y: m.top, width: Math.max(3, x(span.end) - left),
          height: plotHeight, class: "span sev-" + span.severity }, area);
      }
    }
    for (const t of data.exits || []) {
      if (t < a || t > b) continue;
      svgNode("line", { x1: x(t), x2: x(t), y1: m.top, y2: m.top + plotHeight, class: "exit" }, area);
    }
    if (scale) {
      for (const series of def.series) {
        if (isHidden(def, series)) continue;
        const path = linePath(line.times, line.values[series.key] || [], first, last, x, y,
          plotWidth);
        if (path) svgNode("path", { d: path, class: "line " + series.cls }, area);
      }
    }
    svgNode("line", { x1: m.left, x2: m.left + plotWidth, y1: m.top + plotHeight,
      y2: m.top + plotHeight, class: "axis" }, svg);

    chart.markers = [];
    const signal = def.series[0];
    if (scale && def.resource && def.markers !== false && !isHidden(def, signal)) {
      for (const marker of data.markers || []) {
        if (marker.resource !== def.resource || marker.t < a || marker.t > b) continue;
        const value = valueNear(line.values[signal.key] || [], line.nearestIndex(marker.t));
        if (value == null) continue;
        const cx = x(marker.t);
        const dot = svgNode("circle", { cx, cy: y(value), r: 5.5, class: "marker" }, svg);
        svgNode("title", {}, dot).textContent = marker.severity.toUpperCase() + " " +
          marker.resource + " signal " + marker.kind + " at +" + elapsed(marker.t) +
          (marker.message ? "\\n" + marker.message : "");
        chart.markers.push({ marker, cx });
      }
    }

    const group = svgNode("g", { class: "cross", visibility: "hidden" }, svg);
    const crossLine = svgNode("line", { x1: 0, x2: 0, y1: m.top, y2: m.top + plotHeight },
      group);
    const dots = def.series.map((series) => svgNode("circle", { r: 3.5,
      class: "dot " + series.cls }, group));
    chart.brush = svgNode("rect", { x: 0, y: m.top, width: 0, height: plotHeight,
      class: "brush", visibility: "hidden" }, svg);
    if (!scale) {
      svgNode("text", { x: m.left + plotWidth / 2, y: m.top + plotHeight / 2, class: "no-data" },
        svg).textContent = "No measurements in this range";
    }
    chart.geom = { m, plotWidth, plotHeight, a, b, x, y, scale, width };
    chart.cross = { group, line: crossLine, dots };
  }

  function drawAll() {
    for (const chart of charts) draw(chart);
    if (resetButton) resetButton.hidden = view[0] === full[0] && view[1] === full[1];
  }

  function zoomTo(start, end) {
    const span = full[1] - full[0];
    const smallest = Math.min(span, Math.max(1, step * 4));
    if (end - start < smallest) {
      const middle = (start + end) / 2;
      start = middle - smallest / 2;
      end = middle + smallest / 2;
    }
    if (start < full[0]) { end += full[0] - start; start = full[0]; }
    if (end > full[1]) { start -= end - full[1]; end = full[1]; }
    view = [Math.max(full[0], start), Math.min(full[1], end)];
    hideCross();
    drawAll();
  }
  function resetZoom() {
    view = full.slice();
    hideCross();
    drawAll();
  }

  function hideCross() {
    for (const chart of charts) {
      if (chart.cross) chart.cross.group.setAttribute("visibility", "hidden");
      chart.tip.hidden = true;
    }
  }
  function showAt(t, source, pointerX) {
    // Every chart's crosshair sits on the nearest main sample; dots and the
    // tooltip use each chart's own nearest sample.
    const at = T[main.nearestIndex(t)];
    for (const chart of charts) {
      const geom = chart.geom;
      const line = chart.def.line;
      if (!geom || !chart.cross || line.times.length === 0) continue;
      if (at < geom.a || at > geom.b) {
        chart.cross.group.setAttribute("visibility", "hidden");
        continue;
      }
      const xx = geom.x(at);
      chart.cross.line.setAttribute("x1", xx);
      chart.cross.line.setAttribute("x2", xx);
      const index = line.nearestIndex(at);
      chart.def.series.forEach((series, position) => {
        const dot = chart.cross.dots[position];
        const value = (line.values[series.key] || [])[index];
        if (value == null || !geom.scale || isHidden(chart.def, series)) {
          dot.setAttribute("visibility", "hidden");
          return;
        }
        dot.setAttribute("visibility", "visible");
        dot.setAttribute("cx", geom.x(line.times[index]));
        dot.setAttribute("cy", geom.y(value));
      });
      chart.cross.group.setAttribute("visibility", "visible");
      if (chart !== source) chart.tip.hidden = true;
    }
    if (source && source.def.line.times.length > 0) {
      fillTip(source, source.def.line.nearestIndex(at), pointerX);
    }
  }
  function fillTip(chart, index, pointerX) {
    const def = chart.def;
    const line = def.line;
    const tip = chart.tip;
    tip.textContent = "";
    const when = clock(line.times[index]);
    html("div", "tip-time", "+" + elapsed(line.times[index]) + (when ? " · " + when : ""), tip);
    for (const series of def.series) {
      if (isHidden(def, series)) continue;
      const value = (line.values[series.key] || [])[index];
      const row = html("div", "tip-row " + series.cls, null, tip);
      html("span", "swatch", null, row);
      html("span", "tip-label", series.label, row);
      html("span", "tip-value", value == null ? "not measured" : FORMAT[def.unit](value), row);
    }
    const warmAt = line.times[index];
    if (Array.isArray(data.warmup) && warmAt >= data.warmup[0] && warmAt < data.warmup[1]) {
      html("div", "tip-message", "Warm-up: leak detection deferred", tip);
    }
    if (def.resource) {
      const at = line.times[index];
      const span = (data.spans || []).find((item) => item.resource === def.resource &&
        at >= item.start && at <= item.end);
      if (span) {
        html("div", "tip-signal sev-" + span.severity,
          span.severity.toUpperCase() + " " + def.resource + " signal", tip);
      }
      const near = chart.markers.find((item) => Math.abs(item.cx - pointerX) <= 8);
      if (near && near.marker.message) html("div", "tip-message", near.marker.message, tip);
    }
    tip.hidden = false;
    const room = chart.plot.clientWidth;
    let left = pointerX + 14;
    if (left + tip.offsetWidth > room - 4) left = Math.max(4, pointerX - 14 - tip.offsetWidth);
    tip.style.left = left + "px";
  }

  let drag = null;
  function pointerX(chart, event) {
    const box = chart.svg.getBoundingClientRect();
    return (event.clientX - box.left) * (chart.geom.width / Math.max(1, box.width));
  }
  function timeAt(chart, px) {
    const geom = chart.geom;
    const clamped = Math.min(Math.max(px, geom.m.left), geom.m.left + geom.plotWidth);
    return geom.a + ((clamped - geom.m.left) / geom.plotWidth) * (geom.b - geom.a);
  }
  for (const chart of charts) {
    const svg = chart.svg;
    svg.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || !chart.geom) return;
      const px = pointerX(chart, event);
      const geom = chart.geom;
      if (px < geom.m.left || px > geom.m.left + geom.plotWidth) return;
      drag = { chart, start: px, current: px };
      if (svg.setPointerCapture) svg.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    svg.addEventListener("pointermove", (event) => {
      if (!chart.geom) return;
      const px = pointerX(chart, event);
      if (drag && drag.chart === chart) {
        const geom = chart.geom;
        drag.current = Math.min(Math.max(px, geom.m.left), geom.m.left + geom.plotWidth);
        const left = Math.min(drag.start, drag.current);
        const size = Math.abs(drag.current - drag.start);
        chart.brush.setAttribute("x", left);
        chart.brush.setAttribute("width", size);
        chart.brush.setAttribute("visibility", size > 2 ? "visible" : "hidden");
      }
      showAt(timeAt(chart, px), chart, px);
    });
    const finish = (event, apply) => {
      if (!drag || drag.chart !== chart) return;
      const { start, current } = drag;
      drag = null;
      chart.brush.setAttribute("visibility", "hidden");
      if (svg.releasePointerCapture && event.pointerId !== undefined) {
        try { svg.releasePointerCapture(event.pointerId); } catch (error) { /* already released */ }
      }
      if (apply && Math.abs(current - start) >= 6) {
        zoomTo(timeAt(chart, Math.min(start, current)), timeAt(chart, Math.max(start, current)));
      }
    };
    svg.addEventListener("pointerup", (event) => finish(event, true));
    svg.addEventListener("pointercancel", (event) => finish(event, false));
    svg.addEventListener("pointerleave", () => { if (!drag) hideCross(); });
    svg.addEventListener("dblclick", resetZoom);
  }

  for (const row of document.querySelectorAll("tr[data-t]")) {
    const start = Number(row.getAttribute("data-t"));
    if (!Number.isFinite(start)) continue;
    row.addEventListener("mouseenter", () => showAt(start, null, 0));
    row.addEventListener("mouseleave", hideCross);
    const end = Number(row.getAttribute("data-t1"));
    if (!Number.isFinite(end)) continue;
    row.tabIndex = 0;
    const zoom = () => {
      const pad = Math.max(5, (end - start) * 0.25, step * 4);
      zoomTo(start - pad, end + pad);
      const trends = document.getElementById("trends");
      if (trends) trends.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    row.addEventListener("click", zoom);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); zoom(); }
    });
  }
  if (resetButton) resetButton.addEventListener("click", resetZoom);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") resetZoom(); });
  let frame = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(drawAll);
  });
  drawAll();
})();
"""


__all__ = ["render_html_report", "write_html_report"]
