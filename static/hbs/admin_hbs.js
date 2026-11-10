// Admin dashboard partials, rendered from /admin/api/status (polled every 2 s).

const admin_worker_template = `
<div class="flex flex-wrap items-center gap-3">
  <h2 class="font-semibold mr-auto">Pipeline worker</h2>
  {{#if worker_alive}}
    <span class="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200">
      <span class="h-2 w-2 rounded-full bg-emerald-500 animate-pulse"></span>online · up {{fmtDuration worker.uptime}}
    </span>
  {{else}}
    <span class="inline-flex items-center gap-1.5 rounded-full bg-rose-50 px-2.5 py-0.5 text-xs font-medium text-rose-700 ring-1 ring-rose-200">
      <span class="h-2 w-2 rounded-full bg-rose-500"></span>offline{{#if worker.heartbeat_at}} · last seen {{fmtDateTime worker.heartbeat_at}}{{/if}}
    </span>
  {{/if}}
</div>
{{#unless worker_alive}}
  <p class="mt-2 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-800">
    The worker isn't running, so nothing is being crawled or processed. Start it with
    <code class="rounded bg-white px-1">systemctl start civic-worker</code> (or <code class="rounded bg-white px-1">civic-tracker worker</code>).
  </p>
{{/unless}}

<!-- Crawler -->
<div class="mt-4 rounded-lg border border-slate-200 p-3">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-sm font-semibold">Crawler</span>
    {{#if (eq worker.crawler_state "RUNNING")}}
      <span class="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 animate-pulse">RUNNING</span>
    {{else if (eq worker.crawler_state "STOPPING")}}
      <span class="rounded-full bg-orange-100 px-2 py-0.5 text-xs font-medium text-orange-800">STOPPING</span>
    {{else}}
      <span class="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">IDLE</span>
    {{/if}}
    <span class="ml-auto flex gap-2">
      {{#if (eq worker.crawler_state "RUNNING")}}
        <button class="cmd-btn rounded-lg bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700" data-command="STOP_CRAWL">⏹ Stop crawl</button>
      {{else}}
        <button class="cmd-btn rounded-lg bg-emerald-600 px-3 py-1 text-xs text-white hover:bg-emerald-700" data-command="CRAWL_NOW">▶ Crawl now</button>
      {{/if}}
    </span>
  </div>
  <p class="mt-1 text-sm text-slate-600 break-all">{{#if worker.crawler_detail}}{{worker.crawler_detail}}{{else}}—{{/if}}</p>
  <p class="mt-1 text-xs text-slate-400">Last crawl finished: {{fmtDateTime worker.last_crawl_finished_at}}</p>
</div>

<!-- Processor -->
<div class="mt-3 rounded-lg border border-slate-200 p-3">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-sm font-semibold">Processor</span>
    {{#if (eq worker.processor_state "RUNNING")}}
      <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800 animate-pulse">RUNNING</span>
    {{else if (eq worker.processor_state "SYNTHESIZING")}}
      <span class="rounded-full bg-purple-100 px-2 py-0.5 text-xs font-medium text-purple-800 animate-pulse">WRITING BIOS</span>
    {{else if (eq worker.processor_state "PAUSED")}}
      <span class="rounded-full bg-orange-100 px-2 py-0.5 text-xs font-medium text-orange-800">PAUSED</span>
    {{else}}
      <span class="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">{{#if worker.processor_state}}{{worker.processor_state}}{{else}}—{{/if}}</span>
    {{/if}}
    <span class="text-xs text-slate-500">{{worker.processor_detail}} · {{queued}} queued</span>
    <span class="ml-auto flex gap-2">
      {{#if scheduler.processor_enabled}}
        <button class="sched-btn rounded-lg border border-slate-300 px-3 py-1 text-xs hover:border-orange-400" data-field="processor_enabled" data-value="false"
                title="Finish the articles in progress, then stop claiming new ones">⏸ Pause after current</button>
        <button class="cmd-btn rounded-lg bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700" data-command="CANCEL_PROCESSING"
                data-confirm="Abort the articles being processed right now? They go back to the queue and the processor pauses."
                title="Abort in-flight articles now (they return to the queue) and pause">⏹ Stop now</button>
      {{else}}
        <button class="sched-btn rounded-lg bg-emerald-600 px-3 py-1 text-xs text-white hover:bg-emerald-700" data-field="processor_enabled" data-value="true">▶ Resume processing</button>
      {{/if}}
    </span>
  </div>
  <div class="mt-2 space-y-1">
    {{#each worker.slots}}
      <div class="flex items-center gap-2 text-xs">
        <span class="w-12 shrink-0 text-slate-400">slot {{slot}}</span>
        {{#if article_id}}
          <span class="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-blue-700">{{stage}}</span>
          <span class="min-w-0 flex-1 truncate text-slate-700">#{{article_id}} {{title}}</span>
          <span class="ml-auto shrink-0 text-slate-400" data-since="{{since}}"></span>
        {{else}}
          <span class="text-slate-400">idle</span>
        {{/if}}
      </div>
    {{/each}}
  </div>
</div>
`;
const admin_worker_compiled = Handlebars.compile(admin_worker_template);

const admin_counters_template = `
<h2 class="mb-3 font-semibold">Database</h2>
<dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
  <dt class="text-slate-500">Sources</dt><dd class="text-right">{{counters.active_sources}} active / {{counters.sources}}</dd>
  <dt class="text-slate-500">Articles</dt><dd class="text-right">{{counters.articles}}</dd>
  <dt class="text-slate-500 pl-3">queued</dt><dd class="text-right">{{counters.pending}} + {{counters.stage1_done}} stage-1</dd>
  <dt class="text-slate-500 pl-3">in progress</dt><dd class="text-right">{{counters.in_progress}}</dd>
  <dt class="text-slate-500 pl-3">processed</dt><dd class="text-right">{{counters.processed}}</dd>
  <dt class="text-slate-500 pl-3">skipped</dt><dd class="text-right">{{counters.skipped}}</dd>
  <dt class="text-slate-500 pl-3">failed</dt><dd class="text-right">{{#if counters.failed}}<a href="/admin/failures" class="text-rose-600 underline">{{counters.failed}}</a>{{else}}0{{/if}}</dd>
  <dt class="text-slate-500">Entities</dt><dd class="text-right">{{counters.entities}}</dd>
  <dt class="text-slate-500 pl-3">with bios</dt><dd class="text-right">{{counters.bios}}</dd>
  <dt class="text-slate-500 pl-3" title="Changed and eligible (3+ events, or 2+ events and an affiliation); written when the queue is idle and every 30 min">bios waiting</dt><dd class="text-right">{{counters.bios_waiting}}</dd>
  <dt class="text-slate-500 pl-3" title="Changed since last bio but not enough events yet for one">not enough events yet</dt><dd class="text-right">{{counters.bios_not_ready}}</dd>
  <dt class="text-slate-500">Events</dt><dd class="text-right">{{counters.events}}</dd>
</dl>
`;
const admin_counters_compiled = Handlebars.compile(admin_counters_template);

const admin_queue_template = `
<h2 class="mb-3 font-semibold">Queue by source</h2>
<div class="overflow-x-auto"><table class="w-full min-w-[34rem] text-sm">
  <thead><tr class="text-left text-xs uppercase tracking-wide text-slate-500">
    <th class="py-1">Source</th><th class="text-right">Queued</th><th class="text-right">Processed</th>
    <th class="text-right" title="Triaged out (off-topic / not local) or no entities found">Skipped</th>
    <th class="text-right">Failed</th><th class="text-right">Newest article</th><th class="text-right">Last crawled</th></tr></thead>
  <tbody>
  {{#each queue_by_source}}
    <tr class="border-t border-slate-100 {{#unless is_active}}text-slate-400{{/unless}}">
      <td class="py-1">{{name}}{{#unless is_active}} <span class="text-xs">(inactive)</span>{{/unless}}</td>
      <td class="text-right">{{queued}}</td><td class="text-right">{{processed}}</td>
      <td class="text-right text-slate-500">{{skipped}}</td>
      <td class="text-right">{{#if failed}}<span class="text-rose-600">{{failed}}</span>{{else}}0{{/if}}</td>
      <td class="text-right">{{fmtDate newest}}</td><td class="text-right">{{fmtDateTime last_crawled_at}}</td>
    </tr>
  {{/each}}
  </tbody>
</table></div>
`;
const admin_queue_compiled = Handlebars.compile(admin_queue_template);

const admin_runs_template = `
<table class="w-full min-w-[40rem] text-sm">
  <thead><tr class="text-left text-xs uppercase tracking-wide text-slate-500">
    <th class="py-1">#</th><th>Job</th><th>Status</th><th>Started</th><th class="text-right">Duration</th>
    <th class="text-right">OK</th><th class="text-right">Failed</th><th></th></tr></thead>
  <tbody>
  {{#each runs}}
    <tr class="border-t border-slate-100">
      <td class="py-1 text-slate-400">{{id}}</td>
      <td>{{job_type}}{{#if source_name}} <span class="text-xs text-slate-500">· {{source_name}}</span>{{/if}}</td>
      <td>
        {{#if (eq status "SUCCESS")}}<span class="rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-800">SUCCESS</span>
        {{else if (eq status "RUNNING")}}<span class="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800 animate-pulse">RUNNING</span>
        {{else if (eq status "CANCELLED")}}<span class="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">CANCELLED</span>
        {{else if (eq status "PARTIAL_FAILURE")}}<span class="rounded bg-orange-100 px-1.5 py-0.5 text-xs text-orange-800">PARTIAL</span>
        {{else}}<span class="rounded bg-rose-100 px-1.5 py-0.5 text-xs text-rose-800">{{status}}</span>{{/if}}
      </td>
      <td class="text-slate-600">{{fmtDateTime started_at}}</td>
      <td class="text-right text-slate-600">{{fmtDuration duration}}</td>
      <td class="text-right">{{items_processed}}</td>
      <td class="text-right">{{#if items_failed}}<span class="text-rose-600">{{items_failed}}</span>{{else}}0{{/if}}</td>
      <td class="text-right"><button class="log-btn text-xs text-blue-600 hover:underline" data-run-id="{{id}}">log</button></td>
    </tr>
  {{else}}
    <tr><td colspan="8" class="py-2 text-slate-400">No runs yet.</td></tr>
  {{/each}}
  </tbody>
</table>
`;
const admin_runs_compiled = Handlebars.compile(admin_runs_template);

const admin_commands_template = `
<table class="w-full min-w-[36rem] text-sm">
  <thead><tr class="text-left text-xs uppercase tracking-wide text-slate-500">
    <th class="py-1">#</th><th>Command</th><th>Status</th><th>Queued</th><th>Result</th></tr></thead>
  <tbody>
  {{#each commands}}
    <tr class="border-t border-slate-100">
      <td class="py-1 text-slate-400">{{id}}</td><td>{{command}}</td>
      <td><span class="text-xs {{#if (eq status "FAILED")}}text-rose-600{{else if (eq status "RUNNING")}}text-amber-600{{else if (eq status "QUEUED")}}text-blue-600{{else}}text-slate-600{{/if}}">{{status}}</span></td>
      <td class="text-slate-600">{{fmtDateTime created_at}}</td>
      <td class="text-slate-600">{{result}}</td>
    </tr>
  {{else}}
    <tr><td colspan="5" class="py-2 text-slate-400">No commands yet.</td></tr>
  {{/each}}
  </tbody>
</table>
`;
const admin_commands_compiled = Handlebars.compile(admin_commands_template);

const admin_alerts_template = `
{{#if alerts.length}}
<div class="mb-4 rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-800">
  <div class="flex items-center gap-2">
    <strong>🚨 {{alerts.length}} job{{plural alerts.length}} failed or partially failed</strong>
    <button id="ack-alerts" class="ml-auto rounded border border-rose-300 bg-white px-2 py-0.5 text-xs hover:bg-rose-100">Dismiss</button>
  </div>
  <ul class="mt-1 list-disc pl-5">
    {{#each alerts}}<li>{{job_type}} #{{id}} — {{status}} ({{items_processed}} ok, {{items_failed}} failed){{#if error_summary}}: {{error_summary}}{{/if}}</li>{{/each}}
  </ul>
</div>
{{/if}}
`;
const admin_alerts_compiled = Handlebars.compile(admin_alerts_template);

// API burst status (worker.burst from the heartbeat). Dollar signs are plain text in the template.
const admin_burst_template = `
{{#unless burst.configured}}
  <p class="rounded-lg bg-amber-50 px-3 py-2 text-amber-800">
    No API key configured. Add <code>BURST_LLM_API_KEY=...</code> (and check <code>BURST_LLM_MODEL</code> and the
    prices) in <code>civic_tracker/.env</code>, then restart <code>civic-worker</code>.
  </p>
{{/unless}}
{{#if burst.active}}
  <div class="flex flex-wrap items-center gap-2">
    <span class="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 animate-pulse">{{#if burst.stopping}}STOPPING{{else}}RUNNING{{/if}}</span>
    <span class="tabular-nums">{{burst.done}} / {{burst.limit}} articles · {{burst.failed}} failed</span>
    <span class="tabular-nums text-slate-600">{{usd burst.spent_usd 4}} of {{usd burst.cap_usd 2}}</span>
    {{#unless burst.stopping}}<button class="cmd-btn ml-auto rounded-lg bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700" data-command="STOP_BURST">⏹ Stop burst</button>{{/unless}}
  </div>
  <div class="mt-2 h-1.5 w-full rounded-full bg-slate-100" role="progressbar" aria-valuenow="{{burst.done}}" aria-valuemax="{{burst.limit}}">
    <div class="h-1.5 rounded-full bg-blue-600" style="width: {{pct burst.done burst.limit}}%"></div>
  </div>
  <div class="mt-2 space-y-1">
    {{#each burst.slots}}
      <div class="flex items-center gap-2 text-xs">
        <span class="w-12 shrink-0 text-slate-400">api {{slot}}</span>
        <span class="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-blue-700">{{stage}}</span>
        <span class="min-w-0 flex-1 truncate text-slate-700">#{{article_id}} {{title}}</span>
        <span class="shrink-0 text-slate-400" data-since="{{since}}"></span>
      </div>
    {{/each}}
  </div>
{{else}}
  <p class="text-slate-600">{{#if burst.last_result}}Last burst ({{fmtDateTime burst.last_finished_at}}): {{burst.last_result}}{{else}}No burst has run since the worker started.{{/if}}</p>
{{/if}}
`;
const admin_burst_compiled = Handlebars.compile(admin_burst_template);
