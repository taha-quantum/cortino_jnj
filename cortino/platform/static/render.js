// Renders a Cortino dashboard config: plain JavaScript, no dependencies, no network beyond
// this preview app. All text goes in through textContent or setAttribute, never as HTML.
// window.cortino.ready resolves when rendering is done (it is replaced on every filter change);
// window.cortino.chartData(id) returns the rows the chart drew.
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var COLORS = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7"];
  var config = null;
  var values = {}; // filter id -> current value ("" = all)
  var rendered = {}; // chart id -> rows drawn
  var seq = {}; // chart id -> latest request number
  var nodes = {}; // chart id -> {chart, body, legend}

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function svgEl(tag, attrs, text) {
    var e = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  function fmt(v) { return typeof v === "number" ? v.toLocaleString() : (v === null || v === undefined ? "" : String(v)); }
  function color(i) { return COLORS[i % COLORS.length]; }
  // Compact axis and data labels: 31642.75 -> "31.6K", 0.25 -> "0.25".
  function short(v) {
    var n = num(v), a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return String(Math.round(n * 100) / 100);
  }
  function tip(node, text) { node.appendChild(svgEl("title", {}, text)); return node; }

  function allChartIds() {
    var ids = [];
    config.pages.forEach(function (p) { p.charts.forEach(function (c) { ids.push(c.id); }); });
    return ids;
  }

  function targets(f) { return f.applies_to === "all" ? allChartIds() : f.applies_to; }

  function fetchRows(datasetId, query) {
    var qs = query.toString();
    return fetch("/datasets/" + encodeURIComponent(datasetId) + (qs ? "?" + qs : "")).then(function (r) {
      if (!r.ok) throw new Error("dataset " + datasetId + ": HTTP " + r.status);
      return r.json();
    });
  }

  function queryFor(chart) {
    var q = new URLSearchParams();
    config.filters.forEach(function (f) {
      if (values[f.id] && targets(f).indexOf(chart.id) >= 0) q.set(f.param, values[f.id]);
    });
    return q;
  }

  function prepare(chart, rows) {
    rows = rows.slice();
    if (chart.sort) {
      var parts = chart.sort.split(" "), col = parts[0], dir = parts[1] === "desc" ? -1 : 1;
      rows.sort(function (a, b) {
        var x = a[col], y = b[col];
        if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
        return String(x).localeCompare(String(y)) * dir;
      });
    }
    if (chart.top_n) rows = rows.slice(0, chart.top_n);
    return rows;
  }

  function ys(chart) { return chart.encodings.y || []; }

  function legend(node, names) {
    node.replaceChildren();
    node.style.display = names.length < 2 ? "none" : "";  // an empty legend must not take the chart's space
    if (names.length < 2) return;
    names.forEach(function (n, i) {
      var s = el("span", {}, "■ " + n);
      s.style.color = color(i);
      node.appendChild(s);
    });
  }

  function maxOf(rows, cols) {
    var m = 0;
    rows.forEach(function (r) { cols.forEach(function (c) { m = Math.max(m, Math.abs(num(r[c]))); }); });
    return m || 1;
  }

  function bars(chart, rows, w, h, horizontal) {
    var s = svgEl("svg", { width: w, height: h });
    var cols = ys(chart), x = chart.encodings.x, max = maxOf(rows, cols);
    // Room for category labels on one side and the value labels past the end of each bar.
    var left = horizontal ? 90 : 8, bottom = horizontal ? 4 : 18, top = horizontal ? 4 : 16, right = horizontal ? 44 : 8;
    var pw = Math.max(1, w - left - right), ph = Math.max(1, h - bottom - top);
    var band = (horizontal ? ph : pw) / Math.max(1, rows.length), bar = band * 0.8 / Math.max(1, cols.length);
    rows.forEach(function (r, i) {
      var start = i * band + band * 0.1;
      cols.forEach(function (c, j) {
        var len = Math.abs(num(r[c])) / max * (horizontal ? pw : ph), off = start + j * bar;
        s.appendChild(tip(svgEl("rect", horizontal
          ? { x: left, y: top + off, width: len, height: bar, fill: color(j) }
          : { x: left + off, y: top + ph - len, width: bar, height: len, fill: color(j) }), fmt(r[x]) + ": " + fmt(r[c])));
        s.appendChild(svgEl("text", horizontal
          ? { x: left + len + 4, y: top + off + bar / 2, "dominant-baseline": "middle", class: "value" }
          : { x: left + off + bar / 2, y: top + ph - len - 4, "text-anchor": "middle", class: "value" }, short(r[c])));
      });
      s.appendChild(svgEl("text", horizontal
        ? { x: left - 4, y: top + start + band * 0.4, "text-anchor": "end", "dominant-baseline": "middle" }
        : { x: left + start + band * 0.4, y: h - 4, "text-anchor": "middle" }, fmt(r[x])));
    });
    return s;
  }

  function lines(chart, rows, w, h, area) {
    var s = svgEl("svg", { width: w, height: h });
    var cols = ys(chart), x = chart.encodings.x, max = maxOf(rows, cols);
    var left = 44, top = 8, pw = Math.max(1, w - left - 12), ph = Math.max(1, h - top - 22);
    var step = pw / Math.max(1, rows.length - 1);
    function yAt(v) { return top + ph - num(v) / max * ph; }
    [0, 0.5, 1].forEach(function (f) {  // value axis: gridlines at 0, half and the maximum
      var y = top + ph - f * ph;
      s.appendChild(svgEl("line", { x1: left, x2: left + pw, y1: y, y2: y, class: "grid" }));
      s.appendChild(svgEl("text", { x: left - 6, y: y, "text-anchor": "end", "dominant-baseline": "middle" }, short(max * f)));
    });
    cols.forEach(function (c, j) {
      var pts = rows.map(function (r, i) { return (left + i * step) + "," + yAt(r[c]); });
      if (area && pts.length) {
        var poly = [left + "," + (top + ph)].concat(pts, [(left + (rows.length - 1) * step) + "," + (top + ph)]);
        s.appendChild(svgEl("polygon", { points: poly.join(" "), fill: color(j), "fill-opacity": 0.3 }));
      }
      s.appendChild(svgEl("polyline", { points: pts.join(" "), fill: "none", stroke: color(j), "stroke-width": 2 }));
      rows.forEach(function (r, i) {
        s.appendChild(tip(svgEl("circle", { cx: left + i * step, cy: yAt(r[c]), r: 3, fill: color(j) }),
          fmt(r[x]) + ": " + fmt(r[c])));
      });
    });
    var every = Math.max(1, Math.ceil(rows.length * 28 / pw));  // thin the category labels when they would overlap
    rows.forEach(function (r, i) {
      if (i % every === 0) s.appendChild(svgEl("text", { x: left + i * step, y: h - 4, "text-anchor": "middle" }, fmt(r[x])));
    });
    return s;
  }

  function pie(chart, rows, w, h, donut) {
    var s = svgEl("svg", { width: w, height: h });
    var v = ys(chart)[0] || chart.encodings.value, x = chart.encodings.x;
    var total = rows.reduce(function (t, r) { return t + Math.abs(num(r[v])); }, 0) || 1;
    var cx = w / 2, cy = h / 2, r = Math.max(1, Math.min(w, h) / 2 - 4), a = -Math.PI / 2;
    rows.forEach(function (row, i) {
      var sweep = Math.abs(num(row[v])) / total * 2 * Math.PI, b = a + sweep;
      var d = sweep >= 2 * Math.PI - 1e-9
        ? "M" + (cx - r) + "," + cy + "a" + r + "," + r + " 0 1,0 " + 2 * r + ",0a" + r + "," + r + " 0 1,0 " + -2 * r + ",0"
        : "M" + cx + "," + cy + "L" + (cx + r * Math.cos(a)) + "," + (cy + r * Math.sin(a)) +
          "A" + r + "," + r + " 0 " + (sweep > Math.PI ? 1 : 0) + ",1 " + (cx + r * Math.cos(b)) + "," + (cy + r * Math.sin(b)) + "Z";
      var slice = svgEl("path", { d: d, fill: color(i) });
      slice.appendChild(svgEl("title", {}, fmt(row[x]) + ": " + fmt(row[v])));
      s.appendChild(slice);
      a = b;
    });
    if (donut) s.appendChild(svgEl("circle", { cx: cx, cy: cy, r: r * 0.55, fill: "#fff" }));
    return s;
  }

  function scatter(chart, rows, w, h) {
    var s = svgEl("svg", { width: w, height: h });
    var x = chart.encodings.x, y = ys(chart)[0];
    var xmax = maxOf(rows, [x]), ymax = maxOf(rows, [y]), pw = Math.max(1, w - 16), ph = Math.max(1, h - 16);
    rows.forEach(function (r) {
      s.appendChild(svgEl("circle", { cx: 8 + num(r[x]) / xmax * pw, cy: 8 + ph - num(r[y]) / ymax * ph, r: 3, fill: color(0) }));
    });
    return s;
  }

  function table(chart, rows) {
    var cols = [chart.encodings.x].concat(ys(chart)).filter(Boolean);
    if (!cols.length && rows.length) cols = Object.keys(rows[0]);
    var t = el("table"), head = el("tr");
    cols.forEach(function (c) { head.appendChild(el("th", {}, c)); });
    t.appendChild(head);
    rows.forEach(function (r) {
      var tr = el("tr");
      cols.forEach(function (c) { tr.appendChild(el("td", {}, fmt(r[c]))); });
      t.appendChild(tr);
    });
    return t;
  }

  function card(chart, rows) {
    var col = chart.encodings.value || ys(chart)[0] || (rows.length ? Object.keys(rows[0])[0] : null);
    return el("div", { class: "card" }, rows.length && col ? fmt(rows[0][col]) : "");
  }

  var DRAW = {
    bar: function (c, r, w, h) { return bars(c, r, w, h, true); },
    column: function (c, r, w, h) { return bars(c, r, w, h, false); },
    line: function (c, r, w, h) { return lines(c, r, w, h, false); },
    area: function (c, r, w, h) { return lines(c, r, w, h, true); },
    pie: function (c, r, w, h) { return pie(c, r, w, h, false); },
    donut: function (c, r, w, h) { return pie(c, r, w, h, true); },
    scatter: scatter,
    table: table,
    card: card
  };

  function draw(chartId) {
    var n = nodes[chartId], chart = n.chart, mine = (seq[chartId] || 0) + 1;
    seq[chartId] = mine;
    return fetchRows(chart.dataset, queryFor(chart)).then(function (rows) {
      if (seq[chartId] !== mine) return;
      rows = prepare(chart, rows);
      rendered[chartId] = rows;
      var pie_ = chart.component === "pie" || chart.component === "donut";
      var plain = chart.component === "table" || chart.component === "card";
      legend(n.legend, pie_ ? rows.map(function (r) { return fmt(r[chart.encodings.x]); }) : plain ? [] : ys(chart));
      var w = chart.w - 10, h = chart.h - 10 - 20 - (n.legend.childNodes.length ? 16 : 0);
      n.body.style.height = Math.max(0, h) + "px";
      n.body.replaceChildren(DRAW[chart.component](chart, rows, Math.max(1, w), Math.max(1, h)));
    }).catch(function (e) {
      if (seq[chartId] !== mine) return;
      delete rendered[chartId];
      n.body.replaceChildren(el("div", { class: "error" }, "Could not load data: " + e.message));
    });
  }

  function filterOptions(f, control) {
    return fetch("/filters/" + encodeURIComponent(f.id) + "/options").then(function (r) {
      return r.ok ? r.json() : [];
    }).then(function (options) {
      options.forEach(function (v) { control.appendChild(el("option", { value: v }, v)); });
      control.value = values[f.id];
    });
  }

  function buildFilter(f, bar) {
    var label = el("label", {}, f.label), control;
    var d = Array.isArray(f.default) ? (f.default.length === 1 ? f.default[0] : "") : (f.default || "");
    values[f.id] = d;
    if (f.control === "date_range") {
      control = el("input", { type: "date", "data-filter-id": f.id, value: d });
    } else {
      control = el("select", { "data-filter-id": f.id });
      control.appendChild(el("option", { value: "" }, "All"));
    }
    control.addEventListener("change", function () {
      values[f.id] = control.value;
      window.cortino.ready = Promise.all(targets(f).map(draw)).then(function () {});
    });
    label.appendChild(control);
    bar.appendChild(label);
    return f.control === "date_range" ? Promise.resolve() : filterOptions(f, control);
  }

  function build(cfg) {
    config = cfg;
    var app = document.getElementById("app");
    document.title = cfg.title;
    app.appendChild(el("h1", {}, cfg.title));
    var bar = el("div", { class: "filters" });
    app.appendChild(bar);
    cfg.pages.forEach(function (p) {
      app.appendChild(el("h2", { class: "page-name" }, p.name));
      var page = el("div", { class: "page", "data-page": p.name });
      page.style.width = p.width + "px";
      page.style.height = p.height + "px";
      p.charts.forEach(function (c) {
        var box = el("div", { class: "chart", "data-chart-id": c.id });
        box.style.left = c.x + "px";
        box.style.top = c.y + "px";
        box.style.width = c.w + "px";
        box.style.height = c.h + "px";
        box.appendChild(el("h3", {}, c.title));
        var lg = el("div", { class: "legend" }), body = el("div", { class: "body" });
        box.appendChild(lg);
        box.appendChild(body);
        nodes[c.id] = { chart: c, body: body, legend: lg };
        page.appendChild(box);
      });
      app.appendChild(page);
    });
    var pending = cfg.filters.map(function (f) { return buildFilter(f, bar); });
    return Promise.all(pending.concat(allChartIds().map(draw))).then(function () {});
  }

  window.cortino = {
    ready: fetch("/config").then(function (r) { return r.json(); }).then(build),
    chartData: function (chartId) { return rendered.hasOwnProperty(chartId) ? rendered[chartId] : null; }
  };
})();
