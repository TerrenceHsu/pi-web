<script setup lang="ts">
import { ref } from "vue"

import type { CredentialView, ProviderProfileView, VisibleProviderId } from "../../types"
import ProfileForm from "./ProfileForm.vue"

const props = defineProps<{
  providerId: VisibleProviderId
  providerDisplayName: string
  profiles: ProviderProfileView[]
  credentials: CredentialView[]
  sessionId: string | null
}>()

// 每个 Provider 同时只允许一个未保存 draft
const showDraft = ref(false)

function findCredential(profile: ProviderProfileView): CredentialView | null {
  return props.credentials.find((c) => c.credential_id === profile.credential_id) ?? null
}

function addProfile() {
  showDraft.value = true
}

function cancelDraft() {
  showDraft.value = false
}

function onSaved() {
  showDraft.value = false
}
</script>

<template>
  <section class="provider-section" :data-provider-id="providerId">
    <header class="provider-section-header">
      <h3 class="provider-display-name">{{ providerDisplayName }}</h3>
      <button
        v-if="!showDraft"
        type="button"
        class="add-profile-btn"
        data-testid="add-profile-btn"
        @click="addProfile"
      >
        + Add profile
      </button>
    </header>

    <div class="profile-list">
      <ProfileForm
        v-for="p in profiles"
        :key="p.id"
        :provider-id="providerId"
        :provider-display-name="providerDisplayName"
        :profile="p"
        :credential="findCredential(p)"
        :session-id="sessionId"
      />

      <ProfileForm
        v-if="showDraft"
        :key="`draft-${providerId}`"
        :provider-id="providerId"
        :provider-display-name="providerDisplayName"
        :profile="null"
        :credential="null"
        :session-id="sessionId"
        @cancel-draft="cancelDraft"
        @saved="onSaved"
      />
    </div>
  </section>
</template>

<style scoped>
.provider-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.provider-section:last-of-type {
  border-bottom: none;
}
.provider-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.provider-display-name {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
}
.add-profile-btn {
  padding: 4px 10px;
  font-size: 12px;
  border: 1px dashed var(--border-strong, #ccc);
  border-radius: 4px;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
}
.add-profile-btn:hover {
  background: var(--border);
}
.profile-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
</style>
