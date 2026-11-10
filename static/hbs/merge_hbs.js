// Entity picker results on the admin merge page (from /admin/api/entities).
const merge_results_template = `
<ul class="max-h-56 overflow-auto rounded border border-slate-200">
  {{#each items}}
  <li><button type="button" class="pick w-full px-2 py-1 text-left hover:bg-blue-50" data-id="{{id}}" data-name="{{canonical_name}}">
    {{canonical_name}} <span class="text-xs text-slate-500">#{{id}} · {{typeLabel entity_type}} · {{n_events}} event{{plural n_events}}</span>
  </button></li>
  {{else}}
  <li class="px-2 py-1 text-slate-400">No matches.</li>
  {{/each}}
</ul>
`;
const merge_results_compiled = Handlebars.compile(merge_results_template);
