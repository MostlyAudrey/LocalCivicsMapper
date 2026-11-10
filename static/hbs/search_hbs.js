// Omnibox live-search dropdown (header). Rendered from /api/suggest.
const search_suggest_template = `
<ul class="max-h-96 overflow-auto py-1 text-sm">
  {{#each items}}
  <li>
    <a href="{{#if (eq kind 'entity')}}/wiki/entity/{{id}}{{else}}/wiki/event/{{id}}{{/if}}"
       class="flex items-center justify-between gap-3 px-3 py-1.5 hover:bg-blue-50">
      <span class="truncate">{{#if (eq kind 'event')}}<span class="text-slate-400">Event ·</span> {{/if}}{{label}}</span>
      <span class="shrink-0 text-xs text-slate-400">{{#if (eq kind 'entity')}}{{typeLabel sub}}{{else}}{{sub}}{{/if}}</span>
    </a>
  </li>
  {{/each}}
  <li class="border-t border-slate-100">
    <a href="/search?q={{urlenc q}}" class="block px-3 py-1.5 text-blue-700 hover:bg-blue-50">Search all results for “{{q}}” →</a>
  </li>
</ul>
`;
const search_suggest_compiled = Handlebars.compile(search_suggest_template);
