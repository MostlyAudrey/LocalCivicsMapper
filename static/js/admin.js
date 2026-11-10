// Admin control center: polls /admin/api/status every 2 s; buttons enqueue worker commands or flip the
// scheduler state. The web app never runs pipeline work itself (see CLAUDE.md).
$(function () {
  if (!$("#worker-status-panel").length) return;
  let last = null;
  let burstDefaultsSet = false;

  function renderBurst(data) {
    const burst = (data.worker && data.worker.burst) || {};
    $("#burst-status").html(admin_burst_compiled({ burst: burst }));
    $("#burst-endpoint").text(burst.model ? `${burst.provider} · ${burst.model}` : "");
    const blocked = !burst.configured || !!burst.active || !data.worker_alive;
    $("#burst-start").prop("disabled", blocked).toggleClass("opacity-50", blocked);
    if (!burstDefaultsSet && burst.default_cap_usd != null) {  // seed the form once from worker settings
      $("#burst-cap").val(burst.default_cap_usd);
      $("#burst-conc").val(burst.default_concurrency);
      burstDefaultsSet = true;
    }
  }

  $("#burst-form").on("submit", function (e) {
    e.preventDefault();
    const params = {
      limit: parseInt($("#burst-limit").val(), 10),
      max_spend_usd: parseFloat($("#burst-cap").val()),
      concurrency: parseInt($("#burst-conc").val(), 10),
    };
    const msg = `Send up to ${params.limit} articles to the paid API, spending at most $${params.max_spend_usd.toFixed(2)}?`;
    if (!window.confirm(msg)) return;
    $("#burst-start").prop("disabled", true);
    postJSON("/admin/api/command", { command: "BURST", params: params })
      .fail(function (x) { alert("Could not start burst: " + ((x.responseJSON && x.responseJSON.error) || x.status)); });
  });

  function render(data) {
    last = data;
    const c = data.counters || {};
    data.queued = (c.pending || 0) + (c.stage1_done || 0);
    $("#worker-status-panel").html(admin_worker_compiled(data));
    $("#counters").html(admin_counters_compiled(data));
    $("#queue-by-source").html(admin_queue_compiled(data));
    $("#job-runs").html(admin_runs_compiled(data));
    $("#commands").html(admin_commands_compiled(data));
    $("#alert-banner").html(admin_alerts_compiled(data));
    renderBurst(data);
    tickElapsed();

    const s = data.scheduler || {};
    const $t = $("#schedule-toggle");
    $t.toggleClass("bg-emerald-500", !!s.is_enabled).toggleClass("bg-slate-300", !s.is_enabled)
      .attr("aria-checked", !!s.is_enabled)
      .find("span").toggleClass("translate-x-5", !!s.is_enabled).toggleClass("translate-x-0.5", !s.is_enabled);
    if (!$("#interval-hours").is(":focus")) $("#interval-hours").val(s.interval_hours);
    const w = data.worker || {};
    $("#schedule-next").text(
      w.crawler_state === "RUNNING" ? "A crawl is running now." :
      s.is_enabled ? (w.next_crawl_at ? "Next crawl: " + fmtDate(w.next_crawl_at, true) : "Next crawl: when the worker starts")
                   : "Auto-crawl is off. Use “Crawl now” to queue new articles.");
  }

  function tickElapsed() {
    $("[data-since]").each(function () {
      const t = Date.parse($(this).data("since"));
      if (!isNaN(t)) $(this).text(fmtDuration((Date.now() - t) / 1000));
    });
  }

  function poll() {
    $.get("/admin/api/status").done(render).always(function () { setTimeout(poll, 2000); });
  }
  poll();
  setInterval(tickElapsed, 1000);

  // Worker commands (crawl now, stop crawl, stop processing, synthesize bios)
  $(document).on("click", ".cmd-btn", function () {
    const $b = $(this);
    if ($b.data("confirm") && !window.confirm($b.data("confirm"))) return;
    let params = $b.data("params") || {};
    if (typeof params === "string") params = JSON.parse(params);
    $b.prop("disabled", true).addClass("opacity-50");
    postJSON("/admin/api/command", { command: $b.data("command"), params: params })
      .fail(function (x) { alert("Command failed: " + (x.responseJSON && x.responseJSON.error || x.status)); })
      .always(function () { setTimeout(function () { $b.prop("disabled", false).removeClass("opacity-50"); }, 1500); });
  });

  // Scheduler state (pause/resume processor)
  $(document).on("click", ".sched-btn", function () {
    const body = {};
    body[$(this).data("field")] = String($(this).data("value")) === "true";
    postJSON("/admin/api/scheduler", body).done(function () { $.get("/admin/api/status").done(render); });
  });

  $("#schedule-toggle").on("click", function () {
    const enabled = !(last && last.scheduler && last.scheduler.is_enabled);
    postJSON("/admin/api/scheduler", { is_enabled: enabled }).done(function () { $.get("/admin/api/status").done(render); });
  });

  $("#interval-form").on("submit", function (e) {
    e.preventDefault();
    postJSON("/admin/api/scheduler", { interval_hours: parseFloat($("#interval-hours").val()) })
      .done(function () { $("#interval-hours").blur(); $.get("/admin/api/status").done(render); })
      .fail(function (x) { alert(x.responseJSON && x.responseJSON.error || "Could not save interval"); });
  });

  $(document).on("click", "#ack-alerts", function () {
    postJSON("/admin/api/alerts/ack").done(function () { $.get("/admin/api/status").done(render); });
  });

  // Job-run log modal
  $(document).on("click", ".log-btn", function () {
    const id = $(this).data("run-id");
    $.get(`/admin/api/jobrun/${id}`).done(function (r) {
      $("#log-title").text(`${r.job_type} #${r.id} — ${r.status}`);
      $("#log-body").text((r.error_summary ? "ERROR: " + r.error_summary + "\n\n" : "") + (r.log_output || "(no log output)"));
      $("#log-modal").removeClass("hidden").addClass("flex");
    });
  });
  $("#log-close, #log-modal").on("click", function (e) {
    if (e.target === this) $("#log-modal").addClass("hidden").removeClass("flex");
  });
});
