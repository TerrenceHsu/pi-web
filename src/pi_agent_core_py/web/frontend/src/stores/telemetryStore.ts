import { computed, ref } from "vue"
import { defineStore } from "pinia"

import * as telemetryApi from "../api/telemetry"
import { ApiError } from "../api/client"
import type {
  TelemetryOutcome,
  TelemetrySpanDetail,
  TelemetrySpanSummary,
  TelemetryStatus,
  TelemetrySummary,
} from "../types/telemetry"

export const useTelemetryStore = defineStore("telemetry", () => {
  const summary = ref<TelemetrySummary | null>(null)
  const spans = ref<TelemetrySpanSummary[]>([])
  const selectedSpan = ref<TelemetrySpanDetail | null>(null)
  const windowHours = ref(24)
  const statusFilter = ref<TelemetryOutcome | "all">("all")
  const accountFilter = ref("")
  const loading = ref(false)
  const detailLoading = ref(false)
  const error = ref<string | null>(null)
  let requestVersion = 0

  const filteredSpans = computed(() => {
    if (statusFilter.value === "all") return spans.value
    return spans.value.filter((span) => {
      const outcome = span.attributes.outcome
      return (typeof outcome === "string" ? outcome : span.status) === statusFilter.value
    })
  })

  async function refresh(): Promise<void> {
    const version = ++requestVersion
    loading.value = true
    error.value = null
    try {
      const dbStatus: TelemetryStatus | undefined =
        statusFilter.value === "error"
          ? "error"
          : statusFilter.value === "running"
            ? "running"
            : undefined
      const [nextSummary, spanResponse] = await Promise.all([
        telemetryApi.getTelemetrySummary(windowHours.value),
        telemetryApi.listTelemetrySpans({
          windowHours: windowHours.value,
          limit: 200,
          status: dbStatus,
          accountId: accountFilter.value || undefined,
        }),
      ])
      if (version !== requestVersion) return
      summary.value = nextSummary
      spans.value = spanResponse.spans
      if (selectedSpan.value && !spans.value.some((span) => span.id === selectedSpan.value?.id)) {
        selectedSpan.value = null
      }
    } catch (cause) {
      if (version === requestVersion) {
        error.value =
          cause instanceof ApiError ? cause.detail : "Telemetry data could not be loaded."
      }
    } finally {
      if (version === requestVersion) loading.value = false
    }
  }

  async function selectSpan(spanId: string): Promise<void> {
    detailLoading.value = true
    error.value = null
    try {
      selectedSpan.value = await telemetryApi.getTelemetrySpan(spanId)
    } catch (cause) {
      error.value = cause instanceof ApiError ? cause.detail : "Span details could not be loaded."
    } finally {
      detailLoading.value = false
    }
  }

  function clearSelection(): void {
    selectedSpan.value = null
  }

  function reset(): void {
    requestVersion += 1
    summary.value = null
    spans.value = []
    selectedSpan.value = null
    loading.value = false
    detailLoading.value = false
    error.value = null
  }

  return {
    summary,
    spans,
    selectedSpan,
    windowHours,
    statusFilter,
    accountFilter,
    loading,
    detailLoading,
    error,
    filteredSpans,
    refresh,
    selectSpan,
    clearSelection,
    reset,
  }
})
