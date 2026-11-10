// Collapsible page sections (<details data-section>, see _macros.html section/side_section):
// jump links, opening the section named in the URL #hash, expand/collapse all, and an in-page search that
// filters each section's items and opens the sections with matches.
window.Sections = (function () {
  // searchable units inside a section; nested matches count toward their outermost item only
  const ITEM = "[data-item], tbody > tr, li";
  let query = "";
  let savedOpen = null;  // open/closed state before a search started, restored when it is cleared

  const $sections = () => $("details[data-section]");

  function open(id, scroll) {
    const el = document.getElementById(id);
    if (!el || el.tagName !== "DETAILS") return;
    el.open = true;
    if (scroll) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function buildNav() {
    const $links = $("#section-links").empty();
    $sections().each(function () {
      $("<a>", { href: "#" + this.id, class: "text-blue-600 hover:underline", "data-target": this.id })
        .append(document.createTextNode($(this).data("section")))
        .append($("<span>", { class: "nav-hits ml-1 hidden rounded-full bg-amber-100 px-1.5 text-[11px] text-amber-800" }))
        .appendTo($links);
    });
  }

  function items($s) {
    return $s.find(ITEM).filter(function () {
      return !$(this).parentsUntil($s).is(ITEM);  // outermost items only
    });
  }

  function apply() {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length && savedOpen === null) savedOpen = $sections().map(function () { return this.open; }).get();
    $sections().each(function (i) {
      const $s = $(this);
      let hits = 0;
      const $items = items($s);
      if ($items.length) {
        $items.each(function () {
          const text = this.textContent.toLowerCase();
          const match = terms.every((t) => text.includes(t));
          $(this).toggleClass("search-miss", !match);
          if (match && terms.length) hits++;
        });
      } else if (terms.length) {
        const text = $s.find("> div").text().toLowerCase();
        hits = terms.every((t) => text.includes(t)) ? 1 : 0;
      }
      const label = terms.length ? String(hits) : "";
      $s.find("> summary .section-hits").text(label).toggleClass("hidden", !terms.length);
      $(`#section-links a[data-target="${this.id}"] .nav-hits`).text(label).toggleClass("hidden", !terms.length);
      $s.toggleClass("opacity-50", terms.length > 0 && hits === 0);
      if (terms.length) this.open = hits > 0;
    });
    if (!terms.length && savedOpen !== null) {
      $sections().each(function (i) { this.open = !!savedOpen[i]; });
      savedOpen = null;
    }
  }

  $(function () {
    if (!$sections().length) { $("#section-nav").remove(); return; }
    buildNav();
    $("#section-links").on("click", "a", function (e) {
      e.preventDefault();
      const id = $(this).data("target");
      open(id, true);
      history.replaceState(null, "", "#" + id);
    });
    $("#sections-open").on("click", () => $sections().prop("open", true));
    $("#sections-close").on("click", () => $sections().prop("open", false));
    let timer = null;
    $("#section-search").on("input", function () {
      clearTimeout(timer);
      const v = this.value;
      timer = setTimeout(() => { query = v.trim(); apply(); }, 150);
    });
    if (location.hash) open(decodeURIComponent(location.hash.slice(1)), true);
    $(window).on("hashchange", () => open(decodeURIComponent(location.hash.slice(1)), true));
  });

  // call after a section's content is (re)loaded by AJAX (the ledger) so an active search applies to it
  return { refresh: function () { if (query) apply(); } };
})();
