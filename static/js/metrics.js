// Performance charts (/admin/metrics). Chart.js 4, reference palette roles from CSS custom properties.
// One y-axis per chart; legends whenever >= 2 series; failures are a status colour + ✕ marker + label.
$(function () {
  const root = document.getElementById("metrics-root");
  if (!root) return;
  const css = (v) => getComputedStyle(root).getPropertyValue(v).trim();
  const C = {
    surface: css("--surface-1"), grid: css("--grid"), text: css("--text-primary"), text2: css("--text-secondary"),
    s1: css("--series-1"), s2: css("--series-2"), s3: css("--series-3"), critical: css("--status-critical"),
  };
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 12;
  Chart.defaults.color = C.text2;
  Chart.defaults.borderColor = C.grid;

  let range = (new URL(window.location)).searchParams.get("range") || "7d";
  const charts = {};

  const minutes = (ms) => (ms == null ? null : ms / 60000);
  const fmtNum = (n, d) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d || 0 }));
  const fmtMin = (ms) => (ms == null ? "—" : `${(ms / 60000).toFixed(1)} min`);
  const timeTick = (v) => new Date(v).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" });

  const pointStyle = (color) => ({
    backgroundColor: color, borderColor: C.surface, borderWidth: 2, pointRadius: 5, pointHoverRadius: 7,
    pointHitRadius: 10,
  });
  const failStyle = {
    backgroundColor: C.critical, borderColor: C.critical, borderWidth: 2, pointStyle: "crossRot",
    pointRadius: 7, pointHoverRadius: 9, pointHitRadius: 10,
  };
  const axis = (title, extra) => Object.assign({
    title: { display: !!title, text: title, color: C.text2 },
    grid: { color: C.grid, drawTicks: false }, border: { display: false }, ticks: { color: C.text2, padding: 6 },
  }, extra || {});

  function draw(id, config) {
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), config);
  }

  function seriesFor(calls, xOf) {
    const pick = (f) => calls.filter(f).map((c) => ({ x: xOf(c), y: minutes(c.duration_ms), c: c }))
                             .filter((p) => p.x != null && p.y != null);
    const local = (c) => (c.provider || "local") === "local";
    const sets = [  // colour = backend; thinking on/off is in the tooltip
      Object.assign({ label: "Article extraction · local server", data: pick((c) => c.kind === "STAGE2_EXTRACT" && local(c) && c.status === "OK") }, pointStyle(C.s1)),
      Object.assign({ label: "Article extraction · API burst", data: pick((c) => c.kind === "STAGE2_EXTRACT" && !local(c) && c.status === "OK") }, pointStyle(C.s2)),
      Object.assign({ label: "Wiki bio", data: pick((c) => c.kind === "STAGE3_BIO" && c.status === "OK") }, pointStyle(C.s3)),
      Object.assign({ label: "Failed / truncated / cancelled", data: pick((c) => c.status !== "OK") }, failStyle),
    ];
    return sets.filter((s) => s.data.length);
  }

  const callTooltip = {
    callbacks: {
      title: (items) => { const c = items[0].raw.c; return (c.article_id ? `#${c.article_id} ` : "") + (c.label || c.kind); },
      label: (item) => {
        const c = item.raw.c;
        const cost = Number(c.cost_usd) > 0 ? ` · $${Number(c.cost_usd).toFixed(4)}` : "";
        return [`${fmtMin(c.duration_ms)} · ${c.status} · ${c.provider || "local"}${c.thinking ? " · thinking" : ""}${cost}`,
                `${fmtNum(c.completion_tokens)} output tok (${fmtNum(c.reasoning_tokens)} reasoning) · ${fmtNum(c.prompt_tokens)} prompt tok`,
                `${c.concurrent_calls || "?"} concurrent · ${new Date(c.started_at).toLocaleString()}`];
      },
    },
  };
  const legend = { position: "top", align: "start", labels: { usePointStyle: true, boxWidth: 8, color: C.text } };

  function render(d) {
    const s = d.summary || {}, q = d.queue || {};
    $("#telemetry-note").html(metrics_note_compiled(d));
    const tiles = [
      { label: "Median local LLM time", value: fmtMin(s.stage2_p50_ms), sub: s.stage2_ok ? `${fmtNum(s.stage2_ok)} articles in range` : "" },
      { label: "Local 90th percentile", value: fmtMin(s.stage2_p90_ms), sub: s.stage2_max_ms ? `max ${fmtMin(s.stage2_max_ms)}` : "" },
      { label: "Local generation speed", value: s.tokens_per_s_p50 ? `${fmtNum(s.tokens_per_s_p50, 1)} tok/s` : "—",
        sub: s.bio_p50_ms ? `median bio ${(s.bio_p50_ms / 1000).toFixed(0)} s` : "per request" },
      { label: "Throughput, last 6 h", value: `${fmtNum(q.articles_per_hour_6h, 1)}/h`,
        sub: `${fmtNum(q.done_24h)} in last 24 h · ${d.local_only ? "local server" : "all backends"}` },
      { label: "Queue", value: fmtNum(q.queued),
        sub: q.eta_hours ? `≈ ${fmtNum(q.eta_hours, 1)} h at ${d.local_only ? "local" : "current"} rate` : `${fmtNum(q.in_progress)} in progress` },
      d.local_only
        ? { label: "Failed requests", value: fmtNum(s.not_ok || 0), sub: "in range" }
        : { label: "API burst spend", value: `$${Number(s.api_spend_usd || 0).toFixed(2)}`,
            sub: s.api_ok ? `${fmtNum(s.api_ok)} articles · $${Number(s.api_cost_per_article).toFixed(4)}/article · median ${fmtMin(s.api_p50_ms)}`
                          : (s.not_ok ? `${fmtNum(s.not_ok)} failed requests` : "no API bursts in range") },
    ];
    $("#metric-tiles").html(metrics_tiles_compiled({ tiles: tiles }));

    const calls = d.calls || [];
    draw("chart-duration", {
      type: "scatter",
      data: { datasets: seriesFor(calls, (c) => Date.parse(c.started_at)) },
      options: { animation: false, plugins: { legend: legend, tooltip: callTooltip },
        scales: { x: axis("", { type: "linear", ticks: { color: C.text2, callback: timeTick, maxTicksLimit: 8 } }),
                  y: axis("minutes", { beginAtZero: true }) } },
    });
    draw("chart-tokens", {
      type: "scatter",
      data: { datasets: seriesFor(calls, (c) => c.completion_tokens) },
      options: { animation: false, plugins: { legend: legend, tooltip: callTooltip },
        scales: { x: axis("output tokens (incl. reasoning)", { type: "linear", beginAtZero: true }),
                  y: axis("minutes", { beginAtZero: true }) } },
    });

    const bc = d.by_concurrency || [];
    draw("chart-concurrency", {
      type: "bar",
      data: { labels: bc.map((r) => String(r.concurrent_calls)),
              datasets: [{ label: "Median minutes", data: bc.map((r) => minutes(r.p50_ms)), backgroundColor: C.s1,
                           maxBarThickness: 24, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom" }] },
      options: { animation: false, plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (i) => { const r = bc[i.dataIndex]; return [`median ${fmtMin(r.p50_ms)}`, `${r.n} requests · ${fmtNum(r.tok_s, 1)} tok/s`]; } } } },
        scales: { x: axis("concurrent LLM requests", { grid: { display: false } }), y: axis("minutes", { beginAtZero: true }) } },
    });

    const tp = d.throughput || [];
    draw("chart-throughput", {
      type: "bar",
      data: { labels: tp.map((r) => timeTick(r.hour)),
              datasets: [{ label: "Processed", data: tp.map((r) => r.processed), backgroundColor: C.s1,
                           maxBarThickness: 24, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom" }] },
      options: { animation: false, plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (i) => { const r = tp[i.dataIndex]; return [`${r.processed} processed`, `${r.skipped} skipped · ${r.failed} failed`]; } } } },
        scales: { x: axis("", { grid: { display: false }, ticks: { color: C.text2, maxTicksLimit: 6, autoSkip: true, maxRotation: 0 } }),
                  y: axis("articles", { beginAtZero: true, ticks: { color: C.text2, precision: 0 } }) } },
    });

    const st = d.stages || {};
    const stageRows = [["Stage 1 NLP", st.stage1_ms], ["Stage 2 LLM", st.llm_ms], ["Resolve + embed", st.resolve_ms]];
    draw("chart-stages", {
      type: "bar",
      data: { labels: stageRows.map((r) => r[0]),
              datasets: [{ label: "Median seconds", data: stageRows.map((r) => (r[1] == null ? null : r[1] / 1000)),
                           backgroundColor: C.s1, maxBarThickness: 24,
                           borderRadius: { topRight: 4, bottomRight: 4 }, borderSkipped: "left" }] },
      options: { indexAxis: "y", animation: false,
        plugins: { legend: { display: false },
                   tooltip: { callbacks: { label: (i) => `median ${fmtNum(i.raw, 1)} s over ${fmtNum(st.n)} articles` } } },
        scales: { x: axis("seconds", { beginAtZero: true }), y: axis("", { grid: { display: false } }) } },
    });

    const rows = calls.slice().reverse().map((c) => Object.assign({ minutes: minutes(c.duration_ms) }, c));
    $("#calls-table").html(metrics_calls_compiled({ rows: rows }));
  }

  function paintFilter() {
    $("#range-filter button").each(function () {
      const on = $(this).data("range") === range;
      $(this).toggleClass("bg-blue-700 text-white", on).toggleClass("text-slate-600", !on).attr("aria-pressed", on);
    });
  }

  // "Local server only" defaults to on and is remembered per browser (a viewing preference, not shared state).
  let localOnly = true;
  try { localOnly = localStorage.getItem("metrics.localOnly") !== "0"; } catch (e) { /* storage blocked: keep default */ }
  $("#local-only").prop("checked", localOnly).on("change", function () {
    localOnly = this.checked;
    try { localStorage.setItem("metrics.localOnly", localOnly ? "1" : "0"); } catch (e) { /* ignore */ }
    load();
  });

  function load() {
    paintFilter();
    const url = new URL(window.location); url.searchParams.set("range", range); window.history.replaceState(null, "", url);
    $.get("/admin/api/metrics", { range: range, local_only: localOnly ? "1" : "0" }).done(render);
  }

  $("#range-filter").on("click", "button", function () { range = $(this).data("range"); load(); });
  load();
  setInterval(load, 60000);  // new requests land every few minutes
});
