// Career & stance ledger entry (entity page). Rendered from /api/entity/<id>/ledger rows.
const ledger_template = `
{{#each rows}}
<article data-item class="rounded-xl border border-slate-200 bg-white p-4">
  <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
    <span>{{fmtDate event_date}}</span>
    <span class="uppercase tracking-wide">{{underscoreToSpace event_type}}</span>
    {{#if salience_score}}
      <span title="Salience score: article citations, event size, stance, quotes, event type and recency">
        📰 Cited in {{entity_article_citations}}{{#if total_event_articles}}/{{total_event_articles}}{{/if}} article{{plural entity_article_citations}} | Salience: {{fixed salience_score 2}}
      </span>
    {{/if}}
  </div>
  <h3 class="mt-1 font-serif text-lg leading-snug">
    <a href="/wiki/event/{{event_id}}" class="hover:text-blue-700">{{event_title}}</a>
  </h3>
  {{#if commentary_only}}
    <p class="mt-1 text-xs text-amber-700">Only supported by commentary or third-party claims, not by news reporting.</p>
  {{else if provisional}}
    <p class="mt-1 text-xs text-amber-700">Provisional: this event has so far appeared only in opinion pieces.</p>
  {{/if}}
  <div class="mt-1 flex flex-wrap items-center gap-2 text-sm">
    <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-semibold tracking-wide ring-1 ring-inset {{stanceColor stance}}">{{stance}}</span>
    <span class="italic text-slate-600">{{role_in_event}}</span>
  </div>
  <p class="mt-2 text-sm text-slate-700 wiki-prose">{{{action_html}}}</p>
  {{#each direct_quotes}}
    <blockquote class="mt-2 border-l-4 border-slate-200 pl-3 text-sm italic text-slate-600">“{{this}}”</blockquote>
  {{/each}}
  <ul class="mt-2 space-y-1 text-xs">
    {{#each records}}
      <li class="flex flex-wrap items-center gap-1.5">
        <a href="{{url}}" target="_blank" rel="noopener noreferrer" class="text-blue-600 hover:underline" title="{{title}}">[Source {{inc @index}}]</a>
        <span class="text-slate-600">{{source_name}}</span>
        {{#if (eq genre "opinion")}}<span class="rounded bg-amber-50 px-1.5 py-0.5 text-amber-800 ring-1 ring-inset ring-amber-200">opinion</span>{{/if}}
        {{#if (eq genre "official_record")}}<span class="rounded bg-sky-50 px-1.5 py-0.5 text-sky-800 ring-1 ring-inset ring-sky-200" title="City council agenda item / staff report">official record</span>{{/if}}
        {{#if (eq evidence_type "claimed")}}<span class="rounded bg-amber-50 px-1.5 py-0.5 text-amber-800 ring-1 ring-inset ring-amber-200">claimed{{#if claimed_by}} by {{claimed_by}}{{/if}}</span>{{/if}}
        {{#if (eq evidence_type "self")}}<span class="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">own statement</span>{{/if}}
        <span class="text-slate-400" title="Evidence weight: source reliability × article type × evidence type">w {{fixed weight 2}}</span>
        {{#if @root.is_admin}}
          <button class="dispute-btn text-rose-600 hover:underline" data-id="{{entity_event_id}}" data-label="{{source_name}}: {{title}}">⚑ dispute</button>
        {{/if}}
      </li>
    {{/each}}
  </ul>
  <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
    {{#each topic_tags}}<a href="/search?topic={{urlenc this}}" class="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600 hover:text-blue-700">#{{this}}</a>{{/each}}
  </div>
</article>
{{else}}
<p class="text-sm text-slate-500">No recorded events{{#if @root.topic}} for this topic{{/if}}.</p>
{{/each}}
`;
const ledger_compiled = Handlebars.compile(ledger_template);

// Admin: disputed (hidden) records for this entity, with restore buttons.
const disputed_template = `
{{#if rows.length}}
<details class="mt-6 rounded-xl border border-rose-200 bg-rose-50/50 p-3 text-sm">
  <summary class="cursor-pointer font-medium text-rose-800">Disputed records ({{rows.length}}) - hidden from the wiki, alliances and bio</summary>
  <ul class="mt-2 space-y-2">
    {{#each rows}}
      <li>
        <div><span class="font-medium">{{event_title}}</span> <span class="text-xs text-slate-500">{{stance}} · {{source_name}} · disputed {{fmtDateTime disputed_at}}</span></div>
        <div class="text-xs text-slate-600">{{action_summary}}</div>
        <div class="text-xs text-rose-700">Reason: {{dispute_note}}</div>
        <button class="restore-btn mt-1 text-xs text-blue-600 hover:underline" data-id="{{entity_event_id}}">↺ restore</button>
      </li>
    {{/each}}
  </ul>
</details>
{{/if}}
`;
const disputed_compiled = Handlebars.compile(disputed_template);
