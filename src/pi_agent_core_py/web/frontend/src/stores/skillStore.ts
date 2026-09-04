// Skill Store —— 管理全局 Skill 列表、enable/disable 与当前 Workspace 选择。
//
// 重要区分：
//   - enabled: 全局启用状态（POST /api/skills/{name}/enable / disable）
//   - selectedSkillNames: 当前 Workspace 持久化选择的 skill names（UI 镜像）
//   - 二者独立：selected 必须是 enabled 的子集才能实际生效

import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as skillsApi from "../api/skills"
import { ApiError } from "../api/client"
import type { SkillSummary } from "../types"

export const useSkillStore = defineStore("skills", () => {
  const skills = ref<SkillSummary[]>([])
  const selectedSkillNames = ref<string[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 全局 enabled 的 skill 名（来自后端 status）。 */
  const enabledSkillNames = computed(() =>
    skills.value.filter((s) => s.status === "enabled").map((s) => s.name),
  )

  async function loadSkills() {
    loading.value = true
    error.value = null
    try {
      const resp = await skillsApi.listSkills()
      skills.value = resp.skills
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    } finally {
      loading.value = false
    }
  }

  async function uploadSkill(file: File | File[] | FileList) {
    error.value = null
    try {
      const resp = await skillsApi.uploadSkill(file)
      // 后端返回的是本次上传成功的 skill；merge 进 list
      const newNames = new Set(resp.skills.map((s) => s.name))
      skills.value = [...resp.skills, ...skills.value.filter((s) => !newNames.has(s.name))]
      return resp
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  async function enableSkill(name: string) {
    error.value = null
    try {
      const resp = await skillsApi.enableSkill(name)
      const idx = skills.value.findIndex((s) => s.name === name)
      if (idx >= 0) {
        skills.value[idx] = { ...skills.value[idx], status: resp.status }
      }
      return resp
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  async function disableSkill(name: string) {
    error.value = null
    try {
      const resp = await skillsApi.disableSkill(name)
      const idx = skills.value.findIndex((s) => s.name === name)
      if (idx >= 0) {
        skills.value[idx] = { ...skills.value[idx], status: resp.status }
      }
      // 同步从 selected 中移除——disabled 的 skill 不应被选中
      selectedSkillNames.value = selectedSkillNames.value.filter((n) => n !== name)
      return resp
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  function toggleSelectedSkill(name: string) {
    const idx = selectedSkillNames.value.indexOf(name)
    if (idx >= 0) {
      selectedSkillNames.value.splice(idx, 1)
    } else {
      selectedSkillNames.value.push(name)
    }
  }

  function setSelectedSkillNames(names: string[]) {
    selectedSkillNames.value = [...names]
  }

  function clearSelected() {
    selectedSkillNames.value = []
  }

  function resetWorkspace() {
    skills.value = []
    selectedSkillNames.value = []
    loading.value = false
    error.value = null
  }

  return {
    skills,
    selectedSkillNames,
    enabledSkillNames,
    loading,
    error,
    loadSkills,
    uploadSkill,
    enableSkill,
    disableSkill,
    toggleSelectedSkill,
    setSelectedSkillNames,
    clearSelected,
    resetWorkspace,
  }
})
