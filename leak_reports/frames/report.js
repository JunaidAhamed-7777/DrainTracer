
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
          (marker.message ? "\n" + marker.message : "");
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
