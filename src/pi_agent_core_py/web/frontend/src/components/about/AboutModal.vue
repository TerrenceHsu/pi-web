<script setup lang="ts">
import { computed, ref, watch } from "vue"

import { downloadWorkerSource, getAboutLicenses } from "../../api/about"
import type { AboutLicensesResponse, OpenSourceComponentView } from "../../types/about"
import ErrorBanner from "../common/ErrorBanner.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import Modal from "../common/Modal.vue"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (event: "close"): void }>()

const details = ref<AboutLicensesResponse | null>(null)
const loading = ref(false)
const downloading = ref(false)
const error = ref<string | null>(null)

const worker = computed<OpenSourceComponentView | null>(
  () =>
    details.value?.components.find((item) => item.component_id === "wiki-parser-worker") ?? null,
)

function safeErrorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof Error && reason.message) return reason.message
  return fallback
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    details.value = await getAboutLicenses()
  } catch (reason) {
    details.value = null
    error.value = safeErrorMessage(reason, "Unable to load license information.")
  } finally {
    loading.value = false
  }
}

async function downloadSource(): Promise<void> {
  const current = worker.value
  if (!current?.source_offer_available || downloading.value) return
  downloading.value = true
  error.value = null
  try {
    await downloadWorkerSource(
      current.source_archive_url,
      `wiki-parser-worker-${current.version}-source.tar.gz`,
    )
  } catch (reason) {
    error.value = safeErrorMessage(reason, "Unable to download Corresponding Source.")
  } finally {
    downloading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) void load()
  },
  { immediate: true },
)
</script>

<template>
  <Modal
    :open="open"
    title="About & Source"
    width="min(720px, 92vw)"
    data-testid="about-modal"
    @close="emit('close')"
  >
    <div v-if="loading" class="about-loading" data-testid="about-loading">
      <LoadingSpinner :size="16" />
      <span>Loading license information…</span>
    </div>

    <ErrorBanner
      v-if="error"
      :message="error"
      dismissible
      data-testid="about-error"
      @dismiss="error = null"
    />

    <template v-if="details">
      <section class="license-card" data-testid="main-app-license">
        <div class="license-heading">
          <strong>{{ details.application.name }}</strong>
          <span class="license-badge">{{ details.application.license_expression }}</span>
        </div>
        <div class="license-meta">Version {{ details.application.version }}</div>
      </section>

      <section v-if="worker" class="license-card" data-testid="worker-license">
        <div class="license-heading">
          <strong>{{ worker.name }}</strong>
          <span class="license-badge copyleft">{{ worker.license_expression }}</span>
        </div>
        <div class="license-meta">
          Version {{ worker.version }} ·
          {{ worker.runtime_ready ? "runtime ready" : "compliance scaffold; runtime not ready" }}
        </div>
        <p class="legal-notice">
          This separately licensed Worker is free software and comes with absolutely no warranty.
          Its license does not replace the main application's MIT license.
        </p>
        <div v-if="worker.source_tree_sha256" class="source-hash">
          Source tree SHA-256: <code>{{ worker.source_tree_sha256 }}</code>
        </div>
        <div class="source-actions">
          <button
            type="button"
            data-testid="download-worker-source"
            :disabled="!worker.source_offer_available || downloading"
            @click="downloadSource"
          >
            {{ downloading ? "Preparing…" : "Download Corresponding Source" }}
          </button>
          <a :href="worker.license_url" target="_blank" rel="noopener">License</a>
          <a :href="worker.notices_url" target="_blank" rel="noopener">Notices</a>
          <a :href="worker.sbom_url" target="_blank" rel="noopener">SPDX SBOM</a>
          <a :href="worker.source_offer_url" target="_blank" rel="noopener">Manifest</a>
        </div>
        <p v-if="!worker.source_offer_available" class="source-unavailable" role="status">
          This deployment has not configured the Worker source archive. The parser runtime must
          remain unavailable until its Corresponding Source is mounted.
        </p>
      </section>

      <p class="legal-footer">{{ details.legal_notice }}</p>
    </template>
  </Modal>
</template>

<style scoped>
.about-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 13px;
}
.license-card {
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}
.license-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--fg);
}
.license-badge {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--border-strong);
  color: var(--fg);
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.license-badge.copyleft {
  background: #e0f2fe;
  color: #075985;
}
.license-meta,
.legal-notice,
.legal-footer,
.source-unavailable {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.license-meta {
  margin-top: 5px;
}
.legal-notice {
  margin: 12px 0;
}
.source-hash {
  margin-bottom: 12px;
  color: var(--muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.source-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.source-actions button {
  padding: 7px 10px;
}
.source-actions a {
  color: var(--accent);
  font-size: 12px;
}
.source-unavailable {
  margin: 10px 0 0;
  color: var(--danger);
}
.legal-footer {
  margin: 0;
}
</style>
