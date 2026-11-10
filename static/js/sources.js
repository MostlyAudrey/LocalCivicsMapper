/* Admin -> Sources: "Add a source" presets fill the name / URL / scrape_config fields. */
$(function () {
  var $form = $("#add-source-form");
  if (!$form.length) return;
  var $cfg = $form.find("[name=scrape_config]");
  var TZ = "America/Los_Angeles";

  function setConfig(obj) { $cfg.val(JSON.stringify(obj, null, 2)); }

  function fillCivicClerk() {
    var city = $.trim($("#cc-city").val());
    var tenant = $.trim($("#cc-tenant").val()).toLowerCase().replace(/\.portal\.civicclerk\.com.*$/, "").replace(/^https?:\/\//, "");
    var bodies = $("#cc-bodies").val().split(",").map($.trim).filter(Boolean);
    if (city) $form.find("[name=name]").val(city + " " + (bodies.length === 1 ? bodies[0] : "City Council & Commissions"));
    if (tenant) $form.find("[name=base_url]").val("https://" + tenant + ".portal.civicclerk.com");
    setConfig({ method: "civicclerk", tenant: tenant, jurisdiction: city, bodies: bodies, history_batch: 50, timezone: TZ });
  }

  $("#source-preset").on("change", function () {
    var p = $(this).val();
    $("#civicclerk-fields").toggleClass("hidden", p !== "civicclerk");
    if (p === "civicclerk") { fillCivicClerk(); return; }
    if (p === "agendacenter") {
      // base URL: the city's site + /AgendaCenter (the site root is usually taken by the city's news source)
      const $u = $form.find("[name=base_url]");
      if ($u.val() && !/\/AgendaCenter\/?$/i.test($u.val())) $u.val($u.val().replace(/\/+$/, "") + "/AgendaCenter");
      setConfig({ method: "agendacenter", jurisdiction: "", categories: [], include_minutes: true,
                  history_batch: 30, timezone: TZ });
      return;
    }
    setConfig(p ? { method: p, timezone: TZ } : { method: "auto", timezone: TZ });
  });
  $("#cc-city, #cc-tenant, #cc-bodies").on("input", fillCivicClerk);
});
