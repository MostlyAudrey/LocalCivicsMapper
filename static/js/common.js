// Shared setup: CSRF header for $.post/$.ajax, Handlebars helpers, omnibox live search.
const CSRF_TOKEN = $('meta[name="csrf-token"]').attr("content");
$.ajaxSetup({
  beforeSend: function (xhr, settings) {
    if (!/^(GET|HEAD|OPTIONS)$/i.test(settings.type)) xhr.setRequestHeader("X-CSRF-Token", CSRF_TOKEN);
  },
});

function postJSON(url, body) {
  return $.ajax({ url: url, type: "POST", data: JSON.stringify(body || {}), contentType: "application/json" });
}

const STANCE_COLORS = {
  SUPPORT: "bg-emerald-100 text-emerald-800 ring-emerald-300",
  OPPOSE: "bg-rose-100 text-rose-800 ring-rose-300",
  APOLOGETIC: "bg-amber-100 text-amber-800 ring-amber-300",
  DEFENSIVE: "bg-orange-100 text-orange-800 ring-orange-300",
  INVESTIGATIVE: "bg-purple-100 text-purple-800 ring-purple-300",
  NEUTRAL: "bg-slate-100 text-slate-700 ring-slate-300",
  PARTICIPANT: "bg-slate-100 text-slate-700 ring-slate-300",
};

function fmtDate(v, withTime) {
  if (!v) return "—";
  const d = new Date(v.length === 10 ? v + "T12:00:00" : v);
  if (isNaN(d)) return v;
  const opts = { year: "numeric", month: "short", day: "numeric" };
  if (withTime === true) Object.assign(opts, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleString(undefined, opts);
}

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

Handlebars.registerHelper("stanceColor", (s) => STANCE_COLORS[s] || STANCE_COLORS.NEUTRAL);
Handlebars.registerHelper("fmtDate", (v) => fmtDate(v));
Handlebars.registerHelper("fmtDateTime", (v) => fmtDate(v, true));
Handlebars.registerHelper("fmtDuration", (v) => fmtDuration(v));
Handlebars.registerHelper("underscoreToSpace", (v) => (v || "").replace(/_/g, " "));
Handlebars.registerHelper("fixed", (v, n) => (v == null ? "" : Number(v).toFixed(n)));
Handlebars.registerHelper("plural", (n) => (Number(n) === 1 ? "" : "s"));
Handlebars.registerHelper("inc", (i) => Number(i) + 1);
Handlebars.registerHelper("eq", (a, b) => a === b);
Handlebars.registerHelper("urlenc", (v) => encodeURIComponent(v || ""));
Handlebars.registerHelper("pct", (a, b) => (b ? Math.min(100, Math.round((100 * (a || 0)) / b)) : 0));
Handlebars.registerHelper("usd", (v, n) => "$" + Number(v || 0).toFixed(typeof n === "number" ? n : 2));
Handlebars.registerHelper("typeLabel", (t) => (t || "").replace(/_/g, " ").toLowerCase());

// ---------------------------------------------------------------- omnibox
$(function () {
  const $box = $("#omnibox"), $results = $("#omnibox-results");
  if (!$box.length) return;
  let timer = null, lastQ = "", active = -1;

  function hide() { $results.addClass("hidden").empty(); active = -1; }

  $box.on("input", function () {
    const q = $box.val().trim();
    clearTimeout(timer);
    if (q.length < 2) { hide(); return; }
    timer = setTimeout(function () {
      lastQ = q;
      $.get("/api/suggest", { q: q }).done(function (items) {
        if ($box.val().trim() !== lastQ) return;
        if (!items.length) { hide(); return; }
        $results.html(search_suggest_compiled({ items: items, q: q })).removeClass("hidden");
        active = -1;
      });
    }, 150);
  });

  $box.on("keydown", function (e) {
    const $items = $results.find("a");
    if (!$items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + $items.length) % $items.length;
      $items.removeClass("bg-blue-50").eq(active).addClass("bg-blue-50");
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      window.location = $items.eq(active).attr("href");
    } else if (e.key === "Escape") {
      hide();
    }
  });

  $(document).on("click", function (e) {
    if (!$(e.target).closest("#omnibox-form").length) hide();
  });
});
