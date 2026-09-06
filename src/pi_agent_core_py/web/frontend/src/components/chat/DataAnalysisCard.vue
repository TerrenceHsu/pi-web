<script setup lang="ts">
import { computed } from "vue"
import type { AnalysisResult as Result } from "../../api/dataAnalysis"
import AnalysisResult from "../workspace/AnalysisResult.vue"

const props = defineProps<{ details: unknown }>()
const result = computed<Result | null>(() => {
  if (!props.details || typeof props.details !== "object") return null
  const raw = props.details as Record<string, any>
  const value = raw.analysis ?? raw.details?.analysis
  if (!value || value.schema_version !== "pi-agent-data-analysis/v1"
      || !/^analysis-[a-f0-9]{32}$/.test(value.run_id) || typeof value.session_id !== "string"
      || !Array.isArray(value.rows) || value.rows.length > 50
      || !value.rows.every((row: unknown) => Array.isArray(row) && row.length <= 256
        && row.every(cell => cell === null || ["string", "number", "boolean"].includes(typeof cell)))
      || !Array.isArray(value.columns) || value.columns.length > 256
      || !value.columns.every((column: unknown) => typeof column === "string")
      || !Array.isArray(value.warnings) || !value.warnings.every((w: unknown) => typeof w === "string")
      || !value.source || typeof value.source !== "object"
      || (value.source.sheets !== undefined && (!Array.isArray(value.source.sheets)
        || !value.source.sheets.every((s: unknown) => typeof s === "string")))) return null
  return value as Result
})
</script>

<template><AnalysisResult v-if="result" :result="result" /></template>
