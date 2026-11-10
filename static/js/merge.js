// Admin merge page: two entity pickers + confirmation.
$(function () {
  function choose($picker, id, name) {
    $picker.find(".picker-id").val(id);
    $picker.find(".picker-chosen").html(`<span class="font-medium"></span> <span class="text-xs text-slate-500">#${Number(id)}</span>`)
      .find(".font-medium").text(name);
    $picker.find(".picker-results").empty();
    refreshButtons();
  }

  function refreshButtons() {
    const s = $("[data-role=source] .picker-id").val(), t = $("[data-role=target] .picker-id").val();
    $("#merge-submit").prop("disabled", !(s && t && s !== t));
    $("#swap-pair").prop("disabled", !(s || t));
  }

  function current($picker) {
    return { id: $picker.find(".picker-id").val(), name: $picker.find(".picker-chosen .font-medium").text() };
  }

  function clear($picker) {
    $picker.find(".picker-id").val("");
    $picker.find(".picker-chosen").empty();
  }

  // keep the entity currently marked as source: swap the two pickers
  $("#swap-pair").on("click", function () {
    const $s = $("[data-role=source]"), $t = $("[data-role=target]");
    const s = current($s), t = current($t);
    if (t.id) choose($s, t.id, t.name); else clear($s);
    if (s.id) choose($t, s.id, s.name); else clear($t);
    refreshButtons();
  });

  // "not duplicates": remember the pair server-side and drop the row
  $(".dismiss-pair").on("click", function () {
    const $b = $(this);
    const msg = `“${$b.data("a-name")}” and “${$b.data("b-name")}” are different entities?\nThis pair won't be suggested again.`;
    if (!window.confirm(msg)) return;
    $b.prop("disabled", true);
    postJSON("/admin/api/entities/not-duplicate", { a_id: $b.data("a-id"), b_id: $b.data("b-id") })
      .done(function () { $b.closest("tr").fadeOut(200, function () { $(this).remove(); }); })
      .fail(function (x) {
        $b.prop("disabled", false);
        alert("Could not dismiss: " + ((x.responseJSON && x.responseJSON.error) || x.status));
      });
  });

  $(".entity-picker").each(function () {
    const $p = $(this);
    let timer = null;
    $p.find(".picker-q").on("input", function () {
      const q = $(this).val().trim();
      clearTimeout(timer);
      if (q.length < 2) { $p.find(".picker-results").empty(); return; }
      timer = setTimeout(function () {
        $.get("/admin/api/entities", { q: q }).done(function (items) {
          $p.find(".picker-results").html(merge_results_compiled({ items: items }));
        });
      }, 150);
    });
    $p.on("click", ".pick", function () { choose($p, $(this).data("id"), $(this).data("name")); });
  });

  // fill the form: the entity with fewer events is the source (deleted), the other is kept
  $(".pick-pair").on("click", function () {
    const d = $(this).data();
    const aKeeps = Number(d.aEvents) >= Number(d.bEvents);
    const src = aKeeps ? { id: d.bId, name: d.bName } : { id: d.aId, name: d.aName };
    const tgt = aKeeps ? { id: d.aId, name: d.aName } : { id: d.bId, name: d.bName };
    choose($("[data-role=source]"), src.id, src.name);
    choose($("[data-role=target]"), tgt.id, tgt.name);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  $("#merge-form").on("submit", function (e) {
    const s = $("[data-role=source] .picker-chosen .font-medium").text();
    const t = $("[data-role=target] .picker-chosen .font-medium").text();
    if (!window.confirm(`Merge “${s}” into “${t}”? “${s}” will be deleted.`)) e.preventDefault();
  });
});
