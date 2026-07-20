/* Spain Housing Prices — interactive choropleth linked to a time series.
 *
 * One dataset (/api/dataset) drives everything. Clicking a region on the map
 * filters the time series to it; the quarter slider recolours the map and moves
 * a marker on the time series — a two-way link, all client-side.
 */
(function () {
  "use strict";

  var MAP_NAME = "spain-ccaa";
  var GEO_URL = "geo/spain-ccaa.geojson";

  // ---- palettes (from the data-viz reference instance) ---------------------
  var PALETTES = {
    light: {
      text: "#0b0b0b", secondary: "#52514e", muted: "#898781",
      grid: "#e1e0d9", baseline: "#c3c2b7", surface: "#fcfcfb",
      region: "#2a78d6", nation: "#898781", noData: "#ecebe6", mapBorder: "#ffffff",
      sequential: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
      diverging: ["#0d366b", "#2a78d6", "#b7d3f6", "#f0efec", "#f3b0b0", "#e34948", "#8f1d1d"],
    },
    dark: {
      text: "#ffffff", secondary: "#c3c2b7", muted: "#898781",
      grid: "#2c2c2a", baseline: "#383835", surface: "#1a1a19",
      region: "#3987e5", nation: "#a5a49c", noData: "#2a2a28", mapBorder: "#1a1a19",
      sequential: ["#12233a", "#173a63", "#1c5cab", "#2a78d6", "#3987e5", "#6da7ec", "#9ec5f4"],
      diverging: ["#86b6ef", "#3987e5", "#1c5cab", "#383835", "#8a3838", "#e66767", "#f2b5b5"],
    },
  };

  var state = {
    data: null,
    code2region: {},   // code -> {name, parent}
    name2code: {},     // geojson feature name -> code
    indicator: null,
    metric: "level",   // level | yoy
    periodIndex: 0,
    selectedCode: null,
  };
  var mapChart, seriesChart;

  // ---- helpers -------------------------------------------------------------
  function theme() {
    var attr = document.documentElement.getAttribute("data-theme");
    if (attr === "dark") return "dark";
    if (attr === "light") return "light";
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function palette() { return PALETTES[theme()]; }

  function periods() { return state.data.periods[state.indicator] || []; }
  function seriesFor(code) {
    var s = state.data.series[state.indicator];
    return (s && s[code]) || null;
  }
  function quarterLabel(iso) {
    var d = new Date(iso + "T00:00:00Z");
    var q = Math.floor(d.getUTCMonth() / 3) + 1;
    return d.getUTCFullYear() + " Q" + q;
  }
  function levelAt(code, idx) {
    var s = seriesFor(code);
    return s && s[idx] != null ? s[idx] : null;
  }
  function yoyAt(code, idx) {
    if (idx < 4) return null;
    var s = seriesFor(code);
    if (!s) return null;
    var now = s[idx], prev = s[idx - 4];
    if (now == null || prev == null || prev === 0) return null;
    return (now / prev - 1) * 100;
  }
  function valueAt(code, idx) {
    return state.metric === "yoy" ? yoyAt(code, idx) : levelAt(code, idx);
  }
  function fmt(v, metric) {
    if (v == null) return "—";
    return metric === "yoy" ? (v >= 0 ? "+" : "") + v.toFixed(1) + "%" : v.toFixed(1);
  }
  function ccaaCodes() {
    return (state.data.regions.ccaa || []).map(function (r) { return r.code; });
  }

  // Global colour scale bounds so scrubbing the slider shows change over time.
  function levelBounds() {
    var lo = Infinity, hi = -Infinity;
    ccaaCodes().forEach(function (code) {
      (seriesFor(code) || []).forEach(function (v) {
        if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      });
    });
    return isFinite(lo) ? [lo, hi] : [0, 1];
  }
  function yoyBound() {
    var m = 0, n = periods().length;
    ccaaCodes().forEach(function (code) {
      for (var i = 4; i < n; i++) {
        var y = yoyAt(code, i);
        if (y != null) m = Math.max(m, Math.abs(y));
      }
    });
    return m || 1;
  }

  // ---- map -----------------------------------------------------------------
  function renderMap() {
    var p = palette();
    var idx = state.periodIndex;
    var metric = state.metric;
    var isYoY = metric === "yoy";
    var bounds = isYoY ? [-yoyBound(), yoyBound()] : levelBounds();

    var data = ccaaCodes().map(function (code) {
      var region = state.code2region[code];
      var v = valueAt(code, idx);
      var selected = code === state.selectedCode;
      return {
        name: region.name,
        value: v == null ? null : Number(v.toFixed(2)),
        code: code,
        itemStyle: {
          borderColor: selected ? p.region : p.mapBorder,
          borderWidth: selected ? 2.4 : 0.6,
          areaColor: v == null ? p.noData : undefined,
        },
      };
    });

    mapChart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        backgroundColor: p.surface,
        borderColor: p.grid,
        textStyle: { color: p.text },
        formatter: function (params) {
          var code = params.data && params.data.code;
          if (!code) return params.name;
          var lvl = levelAt(code, idx), yy = yoyAt(code, idx);
          return (
            "<strong>" + params.name + "</strong><br/>" +
            "Index: " + fmt(lvl, "level") + "<br/>" +
            "YoY: " + fmt(yy, "yoy")
          );
        },
      },
      visualMap: {
        type: "continuous",
        min: bounds[0],
        max: bounds[1],
        calculable: true,
        orient: "horizontal",
        left: "center",
        bottom: 4,
        itemWidth: 12,
        itemHeight: 140,
        precision: isYoY ? 1 : 0,
        text: isYoY ? ["+", "−"] : ["high", "low"],
        textStyle: { color: p.muted, fontSize: 11 },
        inRange: { color: isYoY ? p.diverging : p.sequential },
        outOfRange: { color: [p.noData] },
      },
      series: [{
        type: "map",
        map: MAP_NAME,
        roam: false,
        nameProperty: "name",
        label: { show: false },
        emphasis: {
          label: { show: true, color: p.text, fontSize: 11 },
          itemStyle: { areaColor: undefined, borderColor: p.region, borderWidth: 1.6 },
        },
        select: { disabled: true },
        itemStyle: { borderColor: p.mapBorder, borderWidth: 0.6, areaColor: p.noData },
        data: data,
      }],
    }, { notMerge: true });
  }

  // ---- time series ---------------------------------------------------------
  function seriesValues(code) {
    var n = periods().length;
    var out = [];
    for (var i = 0; i < n; i++) out.push(valueAt(code, i));
    return out;
  }

  function renderSeries() {
    var p = palette();
    var metric = state.metric;
    var labels = periods().map(quarterLabel);
    var focusCode = state.selectedCode || "es";
    var focusName = state.code2region[focusCode].name;

    var lines = [];
    if (state.selectedCode) {
      lines.push({
        name: "España",
        type: "line", showSymbol: false, smooth: false,
        lineStyle: { color: p.nation, width: 1.5, type: "dashed" },
        itemStyle: { color: p.nation },
        emphasis: { focus: "series" },
        data: seriesValues("es"),
        z: 2,
      });
    }
    lines.push({
      name: focusName,
      type: "line", showSymbol: false, smooth: false,
      lineStyle: { color: p.region, width: 2 },
      itemStyle: { color: p.region },
      areaStyle: state.selectedCode ? undefined : { color: p.region, opacity: 0.06 },
      emphasis: { focus: "series" },
      markLine: {
        symbol: "none",
        silent: true,
        lineStyle: { color: p.baseline, width: 1, type: "solid" },
        label: { show: false },
        data: [{ xAxis: state.periodIndex }],
      },
      data: seriesValues(focusCode),
      z: 3,
    });

    seriesChart.setOption({
      backgroundColor: "transparent",
      grid: { left: 8, right: 16, top: 24, bottom: 28, containLabel: true },
      legend: state.selectedCode
        ? { show: true, top: 0, right: 0, textStyle: { color: p.secondary }, itemWidth: 18, itemHeight: 2 }
        : { show: false },
      tooltip: {
        trigger: "axis",
        backgroundColor: p.surface,
        borderColor: p.grid,
        textStyle: { color: p.text },
        axisPointer: { type: "line", lineStyle: { color: p.baseline } },
        valueFormatter: function (v) { return fmt(v == null ? null : v, metric); },
      },
      xAxis: {
        type: "category",
        data: labels,
        boundaryGap: false,
        axisLine: { lineStyle: { color: p.baseline } },
        axisTick: { show: false },
        axisLabel: { color: p.muted, hideOverlap: true },
      },
      yAxis: {
        type: "value",
        scale: metric !== "yoy",
        splitLine: { lineStyle: { color: p.grid } },
        axisLabel: {
          color: p.muted,
          formatter: metric === "yoy" ? "{value}%" : "{value}",
        },
        axisLine: { show: false },
      },
      series: lines,
    }, { notMerge: true });
  }

  // ---- readout + titles ----------------------------------------------------
  function updateReadout() {
    var idx = state.periodIndex;
    var code = state.selectedCode || "es";
    var name = state.code2region[code].name;
    var lvl = levelAt(code, idx), yy = yoyAt(code, idx);
    var cls = yy == null ? "" : yy >= 0 ? "up" : "down";
    document.getElementById("series-title").textContent = name;
    document.getElementById("map-title").textContent =
      state.metric === "yoy" ? "Year-on-year change · " + quarterLabel(periods()[idx])
                             : "Index level · " + quarterLabel(periods()[idx]);
    document.getElementById("readout").innerHTML =
      "Index <strong>" + fmt(lvl, "level") + "</strong>" +
      " &nbsp;·&nbsp; YoY <span class='" + cls + "'>" + fmt(yy, "yoy") + "</span>" +
      " &nbsp;·&nbsp; <span style='color:var(--text-muted)'>" + quarterLabel(periods()[idx]) + "</span>";
    document.getElementById("clear-btn").hidden = !state.selectedCode;
  }

  function renderAll() { renderMap(); renderSeries(); updateReadout(); }

  // ---- selection + shareable URL hash --------------------------------------
  function selectRegion(code) {
    state.selectedCode = code && code !== "es" && state.selectedCode !== code ? code : null;
    writeHash();
    renderAll();
  }

  function writeHash() {
    var parts = [];
    if (state.selectedCode) parts.push("region=" + state.selectedCode);
    if (state.indicator) parts.push("indicator=" + state.indicator);
    if (state.metric !== "level") parts.push("metric=" + state.metric);
    var hash = parts.length ? "#" + parts.join("&") : "";
    if (hash !== window.location.hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search + hash);
    }
  }

  function applyHash() {
    var raw = (window.location.hash || "").replace(/^#/, "");
    if (!raw) return;
    var params = {};
    raw.split("&").forEach(function (pair) {
      var kv = pair.split("=");
      if (kv.length === 2) params[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1]);
    });
    if (params.indicator && state.data.periods[params.indicator]) state.indicator = params.indicator;
    if (params.metric === "yoy" || params.metric === "level") state.metric = params.metric;
    if (params.region && params.region !== "es" && state.code2region[params.region]) {
      state.selectedCode = params.region;
    }
  }

  function reflectControls() {
    document.getElementById("indicator-select").value = state.indicator;
    Array.prototype.forEach.call(
      document.querySelectorAll("#metric-toggle button"),
      function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-metric") === state.metric)); }
    );
  }

  // ---- controls ------------------------------------------------------------
  function buildControls() {
    var sel = document.getElementById("indicator-select");
    state.data.indicators.forEach(function (ind) {
      var o = document.createElement("option");
      o.value = ind.code;
      o.textContent = ind.name;
      sel.appendChild(o);
    });
    sel.value = state.indicator;
    sel.addEventListener("change", function () {
      state.indicator = sel.value;
      var n = periods().length;
      if (state.periodIndex > n - 1) state.periodIndex = n - 1;
      writeHash();
      syncSlider();
      renderAll();
    });

    document.getElementById("metric-toggle").addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-metric]");
      if (!btn) return;
      state.metric = btn.getAttribute("data-metric");
      Array.prototype.forEach.call(this.querySelectorAll("button"), function (b) {
        b.setAttribute("aria-selected", String(b === btn));
      });
      writeHash();
      renderAll();
    });

    var slider = document.getElementById("period-slider");
    slider.addEventListener("input", function () {
      state.periodIndex = Number(slider.value);
      document.getElementById("period-label").textContent = quarterLabel(periods()[state.periodIndex]);
      renderAll();
    });

    document.getElementById("clear-btn").addEventListener("click", function () {
      state.selectedCode = null;
      renderAll();
    });

    document.getElementById("theme-toggle").addEventListener("click", function () {
      document.documentElement.setAttribute("data-theme", theme() === "dark" ? "light" : "dark");
      renderAll();
    });
  }

  function syncSlider() {
    var slider = document.getElementById("period-slider");
    var n = periods().length;
    slider.max = String(Math.max(0, n - 1));
    slider.value = String(state.periodIndex);
    document.getElementById("period-label").textContent =
      n ? quarterLabel(periods()[state.periodIndex]) : "—";
  }

  // ---- header / banner -----------------------------------------------------
  function fillChrome() {
    var d = state.data;
    var badge = document.getElementById("mode-badge");
    badge.hidden = false;
    badge.textContent = d.mode === "live" ? "Live" : "Sample";
    badge.className = "badge " + (d.mode === "live" ? "live" : "sample");
    if (d.updated_at) {
      document.getElementById("updated").textContent = "Latest: " + quarterLabel(d.updated_at);
    }
    document.getElementById("footer-note").textContent = d.source_note || "";
    var banner = document.getElementById("sample-banner");
    if (d.mode !== "live") {
      banner.hidden = false;
      banner.textContent = "⚠ " + (d.source_note || "Showing sample data.");
    }
  }

  // ---- boot ----------------------------------------------------------------
  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(url + " -> " + r.status);
      return r.json();
    });
  }

  function boot() {
    mapChart = echarts.init(document.getElementById("map"));
    seriesChart = echarts.init(document.getElementById("series"));
    window.addEventListener("resize", function () {
      mapChart.resize();
      seriesChart.resize();
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (!document.documentElement.getAttribute("data-theme")) renderAll();
    });

    // Dataset first (fall back to the bundled sample file if the API is absent).
    fetchJSON("api/dataset")
      .catch(function () { return fetchJSON("sample-dataset.json"); })
      .then(function (data) {
        state.data = data;
        state.indicator = data.indicators[0].code;
        (data.regions.ccaa || []).forEach(function (r) {
          state.code2region[r.code] = { name: r.name, parent: r.parent };
        });
        state.code2region[data.nation.code] = { name: data.nation.name, parent: null };
        applyHash(); // deep-link: region / indicator / metric from the URL
        state.periodIndex = Math.max(0, periods().length - 1);
        fillChrome();
        buildControls();
        reflectControls();
        syncSlider();
        return fetchJSON(GEO_URL);
      })
      .then(function (geo) {
        geo.features.forEach(function (f) {
          state.name2code[f.properties.name] = f.properties.code;
        });
        echarts.registerMap(MAP_NAME, geo);
        mapChart.on("click", function (params) {
          var code = state.name2code[params.name];
          if (code) selectRegion(code);
        });
        renderAll();
      })
      .catch(function (err) {
        document.getElementById("map").innerHTML =
          "<p style='padding:20px;color:var(--text-secondary)'>Failed to load data: " +
          (err && err.message ? err.message : err) + "</p>";
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
