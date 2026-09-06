<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from "vue"

import { useTelemetryStore } from "../../stores/telemetryStore"
import type { TelemetrySpanSummary } from "../../types/telemetry"
import ExecutionAdmin from "./ExecutionAdmin.vue"

const emit = defineEmits<{ (event: "close"): void }>()
const telemetry = useTelemetryStore()
const summary = computed(() => telemetry.summary)
const maxTimelineTotal = computed(() =>
  Math.max(1, ...(summary.value?.timeline.map((bucket) => bucket.total) ?? [1])),
)

let refreshTimer: number | null = null

function formatNumber(value: number | undefined): string {
  return new Intl.NumberFormat().format(value ?? 0)
}

function formatPercent(value: number | undefined): string {
  return `${((value ?? 0) * 100).toFixed(1)}%`
}

function formatDuration(value: number | null | undefined): string {
  if (value == null) return "Running"
  if (value < 1000) return `${value} ms`
  return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`
}

function formatTime(value: number): string {
  return new Date(value).toLocaleString([], {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
}

function outcome(span: TelemetrySpanSummary): string {
  const value = span.attributes.outcome
  return typeof value === "string" ? value : span.status
}

function attributeText(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ")
  if (typeof value === "object" && value !== null) return JSON.stringify(value)
  return String(value)
}

watch(
  () => [telemetry.windowHours, telemetry.statusFilter, telemetry.accountFilter],
  () => void telemetry.refresh(),
)

onMounted(() => {
  void telemetry.refresh()
  refreshTimer = window.setInterval(() => void telemetry.refresh(), 15_000)
})

onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearInterval(refreshTimer)
  telemetry.clearSelection()
})
</script>

<template>
  <div class="telemetry-dashboard" data-testid="telemetry-dashboard">
    <header class="telemetry-header">
      <div class="telemetry-title">
        <span class="telemetry-mark" aria-hidden="true">T</span>
        <div>
          <strong>Agent Telemetry</strong>
          <span>Runtime health, model usage and tool activity across accounts</span>
        </div>
      </div>
      <div class="header-actions">
        <span class="privacy-note">Content and credentials are not collected</span>
        <button
          type="button"
          data-testid="telemetry-refresh"
          :disabled="telemetry.loading"
          @click="telemetry.refresh()"
        >
          {{ telemetry.loading ? "Refreshing…" : "Refresh" }}
        </button>
        <button type="button" data-testid="telemetry-back-to-chat" @click="emit('close')">
          Back to chat
        </button>
      </div>
    </header>

    <main class="telemetry-body">
      <ExecutionAdmin />
      <div v-if="telemetry.error" class="telemetry-error" role="alert">
        <span>{{ telemetry.error }}</span>
        <button type="button" aria-label="Dismiss Telemetry error" @click="telemetry.error = null">
          ×
        </button>
      </div>

      <section class="filters" aria-label="Telemetry filters">
        <label>
          Window
          <select v-model.number="telemetry.windowHours" data-testid="telemetry-window">
            <option :value="1">Last hour</option>
            <option :value="24">Last 24 hours</option>
            <option :value="168">Last 7 days</option>
            <option :value="720">Last 30 days</option>
          </select>
        </label>
        <label>
          Outcome
          <select v-model="telemetry.statusFilter" data-testid="telemetry-status-filter">
            <option value="all">All outcomes</option>
            <option value="completed">Completed</option>
            <option value="error">Errors</option>
            <option value="aborted">Aborted</option>
            <option value="running">Running</option>
          </select>
        </label>
        <label>
          Account
          <select v-model="telemetry.accountFilter" data-testid="telemetry-account-filter">
            <option value="">All accounts</option>
            <option v-for="account in summary?.accounts ?? []" :key="account.id" :value="account.id">
              {{ account.name }}
            </option>
          </select>
        </label>
        <span class="last-updated">
          Updated {{ summary ? formatTime(summary.generated_at_ms) : "—" }}
        </span>
      </section>

      <section class="metric-grid" aria-label="Telemetry overview">
        <article class="metric-card">
          <span>Requests</span>
          <strong>{{ formatNumber(summary?.requests.total) }}</strong>
          <small>{{ formatNumber(summary?.requests.running) }} currently running</small>
        </article>
        <article class="metric-card">
          <span>Error rate</span>
          <strong :class="{ negative: (summary?.requests.error_rate ?? 0) > 0.05 }">
            {{ formatPercent(summary?.requests.error_rate) }}
          </strong>
          <small>{{ formatNumber(summary?.requests.error) }} failed requests</small>
        </article>
        <article class="metric-card">
          <span>P95 latency</span>
          <strong>{{ formatDuration(summary?.requests.p95_duration_ms) }}</strong>
          <small>Avg {{ formatDuration(summary?.requests.average_duration_ms) }}</small>
        </article>
        <article class="metric-card">
          <span>Total tokens</span>
          <strong>{{ formatNumber(summary?.usage.total_tokens) }}</strong>
          <small>
            {{ formatNumber(summary?.usage.input_tokens) }} in ·
            {{ formatNumber(summary?.usage.output_tokens) }} out
          </small>
        </article>
        <article class="metric-card">
          <span>Tool calls</span>
          <strong>{{ formatNumber(summary?.tools.calls) }}</strong>
          <small>{{ formatNumber(summary?.tools.errors) }} tool errors</small>
        </article>
        <article class="metric-card">
          <span>Reported cost</span>
          <strong>${{ (summary?.usage.cost ?? 0).toFixed(4) }}</strong>
          <small>Only when Provider reports pricing</small>
        </article>
      </section>

      <section class="insights-grid">
        <article class="panel timeline-panel">
          <div class="panel-heading">
            <div>
              <strong>Request volume</strong>
              <span>Hourly request outcomes</span>
            </div>
            <div class="legend">
              <span><i class="complete"></i> Completed</span>
              <span><i class="failed"></i> Error</span>
              <span><i class="aborted"></i> Aborted</span>
            </div>
          </div>
          <div v-if="!summary?.timeline.length" class="panel-empty">No requests in this window.</div>
          <div v-else class="timeline" data-testid="telemetry-timeline">
            <div v-for="bucket in summary.timeline" :key="bucket.timestamp_ms" class="timeline-column">
              <div class="bar-stack" :title="`${formatTime(bucket.timestamp_ms)} · ${bucket.total}`">
                <span
                  class="bar complete"
                  :style="{ height: `${(bucket.completed / maxTimelineTotal) * 100}%` }"
                ></span>
                <span
                  class="bar failed"
                  :style="{ height: `${(bucket.error / maxTimelineTotal) * 100}%` }"
                ></span>
                <span
                  class="bar aborted"
                  :style="{ height: `${(bucket.aborted / maxTimelineTotal) * 100}%` }"
                ></span>
              </div>
              <small>{{ new Date(bucket.timestamp_ms).getHours().toString().padStart(2, "0") }}</small>
            </div>
          </div>
        </article>

        <article class="panel compact-panel">
          <div class="panel-heading">
            <div>
              <strong>Top tools</strong>
              <span>Completed tool executions</span>
            </div>
          </div>
          <div v-if="!summary?.tools.top.length" class="panel-empty">No tool calls yet.</div>
          <ol v-else class="rank-list">
            <li v-for="tool in summary.tools.top" :key="tool.name">
              <code>{{ tool.name }}</code>
              <strong>{{ formatNumber(tool.calls) }}</strong>
            </li>
          </ol>
        </article>

        <article class="panel compact-panel">
          <div class="panel-heading">
            <div>
              <strong>Providers</strong>
              <span>Requests by Provider</span>
            </div>
          </div>
          <div v-if="!summary?.providers.length" class="panel-empty">No Provider data yet.</div>
          <ol v-else class="rank-list">
            <li v-for="provider in summary.providers" :key="provider.name">
              <code>{{ provider.name }}</code>
              <strong>{{ formatNumber(provider.requests) }}</strong>
            </li>
          </ol>
        </article>
      </section>

      <section class="panel traces-panel">
        <div class="panel-heading">
          <div>
            <strong>Recent Agent requests</strong>
            <span>{{ telemetry.filteredSpans.length }} traces match the current filters</span>
          </div>
        </div>
        <div class="trace-table-wrap">
          <table class="trace-table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Account</th>
                <th>Provider / model</th>
                <th>Mode</th>
                <th>Outcome</th>
                <th>Duration</th>
                <th>Tokens</th>
                <th>Tools</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="telemetry.loading && telemetry.spans.length === 0">
                <td colspan="8" class="table-empty">Loading traces…</td>
              </tr>
              <tr v-else-if="telemetry.filteredSpans.length === 0">
                <td colspan="8" class="table-empty">No Agent requests match these filters.</td>
              </tr>
              <tr
                v-for="span in telemetry.filteredSpans"
                v-else
                :key="span.id"
                tabindex="0"
                data-testid="telemetry-span-row"
                @click="telemetry.selectSpan(span.id)"
                @keydown.enter="telemetry.selectSpan(span.id)"
              >
                <td>{{ formatTime(span.started_at_ms) }}</td>
                <td>{{ span.attributes.account_name || "—" }}</td>
                <td>
                  <strong>{{ span.attributes.provider || "—" }}</strong>
                  <small>{{ span.attributes.model || "" }}</small>
                </td>
                <td>{{ span.attributes.execution_mode || "direct" }}</td>
                <td>
                  <span :class="['outcome-pill', outcome(span)]">{{ outcome(span) }}</span>
                </td>
                <td>{{ formatDuration(span.duration_ms) }}</td>
                <td>
                  {{ formatNumber(Number(span.attributes.input_tokens ?? 0) + Number(span.attributes.output_tokens ?? 0)) }}
                </td>
                <td>{{ formatNumber(Number(span.attributes.tool_calls ?? 0)) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <div
      v-if="telemetry.selectedSpan || telemetry.detailLoading"
      class="detail-backdrop"
      data-testid="telemetry-detail-backdrop"
      @click.self="telemetry.clearSelection()"
    >
      <aside class="detail-panel" data-testid="telemetry-span-detail">
        <header>
          <div>
            <strong>Trace details</strong>
            <code v-if="telemetry.selectedSpan">{{ telemetry.selectedSpan.id.slice(0, 12) }}</code>
          </div>
          <button type="button" aria-label="Close trace details" @click="telemetry.clearSelection()">×</button>
        </header>
        <div v-if="telemetry.detailLoading" class="detail-loading">Loading trace…</div>
        <template v-else-if="telemetry.selectedSpan">
          <dl class="detail-summary">
            <div><dt>Outcome</dt><dd>{{ outcome(telemetry.selectedSpan) }}</dd></div>
            <div><dt>Duration</dt><dd>{{ formatDuration(telemetry.selectedSpan.duration_ms) }}</dd></div>
            <div><dt>Started</dt><dd>{{ formatTime(telemetry.selectedSpan.started_at_ms) }}</dd></div>
            <div><dt>Trace ID</dt><dd><code>{{ telemetry.selectedSpan.trace_id }}</code></dd></div>
          </dl>
          <section class="detail-section">
            <h3>Attributes</h3>
            <dl class="attribute-list">
              <div v-for="(value, key) in telemetry.selectedSpan.attributes" :key="key">
                <dt>{{ key }}</dt>
                <dd>{{ attributeText(value) }}</dd>
              </div>
            </dl>
          </section>
          <section class="detail-section">
            <h3>Event timeline</h3>
            <div v-if="!telemetry.selectedSpan.events.length" class="panel-empty">No events.</div>
            <ol v-else class="event-list">
              <li v-for="(event, index) in telemetry.selectedSpan.events" :key="`${event.timestamp_ms}-${index}`">
                <span class="event-dot"></span>
                <div>
                  <strong>{{ event.name }}</strong>
                  <small>+{{ event.timestamp_ms - telemetry.selectedSpan.started_at_ms }} ms</small>
                  <dl>
                    <div v-for="(value, key) in event.attributes" :key="key">
                      <dt>{{ key }}</dt><dd>{{ attributeText(value) }}</dd>
                    </div>
                  </dl>
                </div>
              </li>
            </ol>
          </section>
        </template>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.telemetry-dashboard {
  min-height: 100vh;
  overflow: auto;
  background: #f4f6f8;
  color: var(--fg);
}
.telemetry-header {
  position: sticky;
  z-index: 10;
  top: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(12px);
}
.telemetry-title,
.header-actions,
.panel-heading,
.legend,
.filters {
  display: flex;
  align-items: center;
}
.telemetry-title { gap: 11px; }
.telemetry-title > div { display: flex; flex-direction: column; }
.telemetry-title strong { font-size: 15px; }
.telemetry-title span:not(.telemetry-mark) { color: var(--muted); font-size: 11px; }
.telemetry-mark {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 10px;
  background: #0f766e;
  color: white;
  font-weight: 700;
}
.header-actions { gap: 8px; }
.privacy-note { margin-right: 8px; color: #0f766e; font-size: 11px; }
.telemetry-body { max-width: 1500px; margin: 0 auto; padding: 20px 24px 42px; }
.telemetry-error {
  display: flex;
  justify-content: space-between;
  margin-bottom: 14px;
  padding: 9px 12px;
  border: 1px solid #fecaca;
  border-radius: 8px;
  background: #fef2f2;
  color: #b91c1c;
}
.telemetry-error button { border: 0; background: transparent; color: inherit; }
.filters {
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}
.filters label { color: var(--muted); font-size: 11px; }
.filters select {
  min-width: 132px;
  margin-left: 6px;
  padding: 6px 28px 6px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: white;
  color: var(--fg);
}
.last-updated { margin-left: auto; color: var(--muted); font-size: 11px; }
.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(145px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.metric-card,
.panel {
  border: 1px solid #e3e7eb;
  border-radius: 10px;
  background: white;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
}
.metric-card { display: flex; flex-direction: column; padding: 14px 16px; }
.metric-card > span { color: var(--muted); font-size: 11px; }
.metric-card > strong { margin: 4px 0 1px; font-size: 23px; font-variant-numeric: tabular-nums; }
.metric-card > strong.negative { color: var(--danger); }
.metric-card small { overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.insights-grid {
  display: grid;
  grid-template-columns: minmax(400px, 2fr) minmax(180px, 1fr) minmax(180px, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}
.panel { min-width: 0; }
.panel-heading { justify-content: space-between; gap: 12px; padding: 13px 15px; border-bottom: 1px solid var(--border); }
.panel-heading > div:first-child { display: flex; flex-direction: column; }
.panel-heading strong { font-size: 12px; }
.panel-heading span { color: var(--muted); font-size: 10px; }
.legend { gap: 10px; }
.legend span { display: flex; align-items: center; gap: 4px; }
.legend i { width: 7px; height: 7px; border-radius: 2px; }
.legend .complete,
.bar.complete { background: #0f766e; }
.legend .failed,
.bar.failed { background: #dc2626; }
.legend .aborted,
.bar.aborted { background: #d97706; }
.timeline {
  display: flex;
  height: 145px;
  align-items: flex-end;
  gap: 4px;
  overflow-x: auto;
  padding: 17px 14px 9px;
}
.timeline-column { display: flex; min-width: 17px; height: 100%; flex: 1; flex-direction: column; align-items: center; gap: 4px; }
.bar-stack { display: flex; width: 100%; min-width: 8px; height: 112px; flex-direction: column-reverse; justify-content: flex-start; overflow: hidden; border-radius: 3px 3px 1px 1px; background: #eef1f4; }
.bar { display: block; width: 100%; min-height: 0; }
.timeline-column small { color: var(--muted); font-size: 8px; }
.panel-empty,
.table-empty,
.detail-loading { padding: 28px; color: var(--muted); text-align: center; }
.rank-list { max-height: 154px; margin: 0; padding: 7px 14px 9px; overflow: auto; list-style: none; }
.rank-list li { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 0; border-bottom: 1px solid #f0f1f3; }
.rank-list code { overflow: hidden; background: transparent; text-overflow: ellipsis; white-space: nowrap; }
.rank-list strong { font-size: 11px; }
.trace-table-wrap { max-height: 420px; overflow: auto; }
.trace-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.trace-table th { position: sticky; z-index: 1; top: 0; padding: 8px 12px; background: #f8fafb; color: var(--muted); text-align: left; font-weight: 600; }
.trace-table td { padding: 9px 12px; border-top: 1px solid #eef0f2; font-variant-numeric: tabular-nums; }
.trace-table tbody tr:not(:has(.table-empty)) { cursor: pointer; }
.trace-table tbody tr:not(:has(.table-empty)):hover { background: #f7faf9; }
.trace-table td strong,
.trace-table td small { display: block; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-table td small { color: var(--muted); }
.outcome-pill { display: inline-block; padding: 1px 7px; border-radius: 999px; background: #edf0f2; color: #475569; }
.outcome-pill.completed { background: #dff5ef; color: #0f766e; }
.outcome-pill.error { background: #fee2e2; color: #b91c1c; }
.outcome-pill.aborted { background: #fef3c7; color: #92400e; }
.outcome-pill.running { background: #dbeafe; color: #1d4ed8; }
.detail-backdrop { position: fixed; z-index: 80; inset: 0; display: flex; justify-content: flex-end; background: rgba(15, 23, 42, 0.28); }
.detail-panel { width: min(540px, 94vw); height: 100%; overflow: auto; border-left: 1px solid var(--border); background: white; box-shadow: -10px 0 35px rgba(15, 23, 42, 0.16); }
.detail-panel > header { position: sticky; z-index: 2; top: 0; display: flex; align-items: center; justify-content: space-between; padding: 14px 17px; border-bottom: 1px solid var(--border); background: white; }
.detail-panel > header > div { display: flex; flex-direction: column; }
.detail-panel > header code { padding: 0; background: transparent; color: var(--muted); }
.detail-panel > header button { border: 0; background: transparent; font-size: 21px; }
.detail-summary { display: grid; grid-template-columns: 1fr 1fr; margin: 0; padding: 14px 17px; gap: 10px; }
.detail-summary div,
.attribute-list div { min-width: 0; }
.detail-summary dt,
.attribute-list dt,
.event-list dt { color: var(--muted); font-size: 9px; text-transform: uppercase; }
.detail-summary dd,
.attribute-list dd,
.event-list dd { margin: 1px 0 0; overflow-wrap: anywhere; font-size: 11px; }
.detail-section { padding: 0 17px 18px; }
.detail-section h3 { margin: 4px 0 9px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.attribute-list { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 0; padding: 11px; border-radius: 8px; background: #f6f8f9; }
.event-list { margin: 0; padding: 0; list-style: none; }
.event-list > li { position: relative; display: grid; grid-template-columns: 12px 1fr; gap: 7px; padding-bottom: 13px; }
.event-list > li::before { position: absolute; top: 8px; bottom: -1px; left: 4px; width: 1px; content: ""; background: var(--border); }
.event-list > li:last-child::before { display: none; }
.event-dot { z-index: 1; width: 9px; height: 9px; margin-top: 4px; border: 2px solid white; border-radius: 50%; background: #0f766e; box-shadow: 0 0 0 1px #0f766e; }
.event-list strong { font-size: 11px; }
.event-list small { margin-left: 7px; color: var(--muted); font-size: 9px; }
.event-list dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3px 9px; margin: 4px 0 0; }
.event-list dl div { min-width: 0; }
@media (max-width: 1100px) {
  .metric-grid { grid-template-columns: repeat(3, 1fr); }
  .insights-grid { grid-template-columns: 1fr 1fr; }
  .timeline-panel { grid-column: 1 / -1; }
}
@media (max-width: 700px) {
  .telemetry-header { align-items: flex-start; padding: 12px 14px; }
  .header-actions { flex-wrap: wrap; justify-content: flex-end; }
  .privacy-note { display: none; }
  .telemetry-body { padding: 14px; }
  .metric-grid { grid-template-columns: repeat(2, 1fr); }
  .insights-grid { grid-template-columns: 1fr; }
  .timeline-panel { grid-column: auto; }
  .last-updated { width: 100%; margin-left: 0; }
}
</style>
