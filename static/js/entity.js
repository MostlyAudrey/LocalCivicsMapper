// Entity dossier: salience/chronological sort toggle + topic filter for the ledger.

// Admin: edit aliases (names the importer files under this entity).
$(function () {
  const $ed = $("#alias-editor");
  if (!$ed.length) return;
  const url = `/admin/api/entities/${$ed.data("entity-id")}/aliases`;
  const fail = (x) => alert("Could not update aliases: " + ((x.responseJSON && x.responseJSON.error) || x.status));
  $ed.on("click", ".alias-remove", function () {
    const alias = $(this).data("alias");
    if (!window.confirm(`Remove the alias “${alias}”?\nNew mentions of that name will no longer be filed here.`)) return;
    postJSON(url, { remove: alias }).done(() => location.reload()).fail(fail);
  });
  $("#alias-add").on("click", function () {
    const alias = (window.prompt("Add an alias (another name for this entity):") || "").trim();
    if (alias) postJSON(url, { add: alias }).done(() => location.reload()).fail(fail);
  });
});

$(function () {
  const $ledger = $("#ledger");
  if (!$ledger.length) return;
  const entityId = $ledger.data("entity-id");
  const isAdmin = String($ledger.data("admin")) === "1";
  let sort = $ledger.data("sort") || "salience";
  let topic = $ledger.data("topic") || "";
  let commentary = false;  // default view: news reporting only

  function loadDisputed() {
    if (!isAdmin) return;
    $.get(`/admin/api/entity/${entityId}/disputed`).done(function (rows) {
      $("#ledger-disputed").html(disputed_compiled({ rows: rows }));
    });
  }

  $("#ledger-commentary").on("change", function () { commentary = this.checked; load(); });

  $("#ledger-rows").on("click", ".dispute-btn", function () {
    const id = $(this).data("id");
    const note = window.prompt(`Dispute this record?\n\n${$(this).data("label")}\n\nIt will be hidden from the wiki, ` +
                               "alliances and the bio. Reason (required):");
    if (!note || !note.trim()) return;
    postJSON(`/admin/api/entity-event/${id}/dispute`, { note: note.trim() })
      .done(function () { load(); loadDisputed(); })
      .fail(function (x) { alert((x.responseJSON && x.responseJSON.error) || "Could not dispute"); });
  });

  $("#ledger-disputed").on("click", ".restore-btn", function () {
    postJSON(`/admin/api/entity-event/${$(this).data("id")}/restore`).done(function () { load(); loadDisputed(); });
  });

  function paintControls() {
    $("#ledger-sort button").each(function () {
      const on = $(this).data("sort") === sort;
      $(this).toggleClass("bg-blue-700 text-white", on).toggleClass("text-slate-600", !on);
    });
    $("#ledger-topics button").each(function () {
      const on = String($(this).data("topic")) === String(topic);
      $(this).toggleClass("bg-blue-700 text-white ring-blue-700", on)
             .toggleClass("bg-white text-slate-600 ring-slate-300", !on);
    });
  }

  function load() {
    paintControls();
    const url = new URL(window.location);
    url.searchParams.set("sort", sort);
    if (topic) url.searchParams.set("topic", topic); else url.searchParams.delete("topic");
    window.history.replaceState(null, "", url);
    $("#ledger-rows").css("opacity", 0.5);
    $.get(`/api/entity/${entityId}/ledger`, { sort: sort, topic: topic, commentary: commentary ? "1" : "0" })
      .done(function (data) {
        $("#ledger-rows").html(ledger_compiled({ rows: data.rows, topic: topic, is_admin: data.is_admin })).css("opacity", 1);
        if (window.Sections) Sections.refresh();
      })
      .fail(function () {
        $("#ledger-rows").html('<p class="text-sm text-rose-600">Could not load the ledger.</p>').css("opacity", 1);
      });
  }

  // topic tags are hidden until asked for (shown when a topic filter is active)
  $("#ledger-topics-toggle").on("click", function () {
    const show = $("#ledger-topics").hasClass("hidden");
    $("#ledger-topics").toggleClass("hidden", !show).toggleClass("flex", show);
    $(this).attr("aria-expanded", String(show))
      .text((show ? "▾" : "▸") + $(this).text().replace(/^[▾▸]/, ""));
  });

  $("#ledger-sort").on("click", "button", function () { sort = $(this).data("sort"); load(); });
  $("#ledger-topics").on("click", "button", function () { topic = String($(this).data("topic") || ""); load(); });
  load();
  loadDisputed();
});
