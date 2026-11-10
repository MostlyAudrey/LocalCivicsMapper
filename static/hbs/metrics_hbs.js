// Performance page partials (/admin/metrics), rendered from /admin/api/metrics.

const metrics_tiles_template = `
{{#each tiles}}
<div class="rounded-xl border border-slate-200 bg-white px-4 py-3">
  <div class="text-xs text-slate-500">{{label}}</div>
  <div class="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{{value}}</div>
  {{#if sub}}<div class="text-xs text-slate-500">{{sub}}</div>{{/if}}
</div>
{{/each}}
`;
const metrics_tiles_compiled = Handlebars.compile(metrics_tiles_template);

const metrics_note_template = `
{{#if telemetry_missing}}
<div class="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-800">
  Per-request LLM timing isn't being recorded yet: apply <code>sql/unapplied/v1.3_LLMTelemetry.sql</code> and restart
  <code>civic-worker</code>. Hourly throughput below still works.
</div>
{{else}}{{#unless calls.length}}
<div class="mb-4 rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm text-slate-600">
  No LLM requests recorded in this range yet. The first ones appear when the worker finishes an article.
</div>
{{/unless}}{{/if}}
`;
const metrics_note_compiled = Handlebars.compile(metrics_note_template);

const metrics_calls_template = `
<table class="w-full min-w-[52rem] text-sm">
  <thead><tr class="text-left text-xs uppercase tracking-wide text-slate-500">
    <th class="py-1">Started</th><th>Kind</th><th>Thinking</th><th>Status</th><th class="text-right">Minutes</th>
    <th class="text-right">Concurrent</th><th class="text-right">Prompt tok</th><th class="text-right">Output tok</th>
    <th class="text-right">Reasoning tok</th><th>Article / entity</th></tr></thead>
  <tbody>
  {{#each rows}}
    <tr class="border-t border-slate-100">
      <td class="py-1 whitespace-nowrap text-slate-600">{{fmtDateTime started_at}}</td>
      <td>{{kind}}</td><td>{{#if thinking}}on{{else}}off{{/if}}</td>
      <td>{{#if (eq status "OK")}}OK{{else}}<span class="font-medium text-slate-900">✕ {{status}}</span>{{/if}}</td>
      <td class="text-right tabular-nums">{{fixed minutes 1}}</td>
      <td class="text-right tabular-nums">{{concurrent_calls}}</td>
      <td class="text-right tabular-nums">{{prompt_tokens}}</td>
      <td class="text-right tabular-nums">{{completion_tokens}}</td>
      <td class="text-right tabular-nums">{{reasoning_tokens}}</td>
      <td class="max-w-xs truncate">{{#if article_id}}#{{article_id}} {{/if}}{{label}}</td>
    </tr>
  {{else}}
    <tr><td colspan="10" class="py-2 text-slate-400">No requests in this range.</td></tr>
  {{/each}}
  </tbody>
</table>
`;
const metrics_calls_compiled = Handlebars.compile(metrics_calls_template);
