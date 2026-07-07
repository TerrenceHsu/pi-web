// Skills API——P0-4 Step 1 上传 / 启用 / 禁用 / 详情。

import type {
  SkillDetailResponse,
  SkillListResponse,
  SkillToggleResponse,
  SkillUploadResponse,
} from "../types"
import { requestJson, uploadForm } from "./client"

/** GET /api/skills——列表。includePrompt 默认 false。 */
export function listSkills(includePrompt = false) {
  return requestJson<SkillListResponse>(
    "/api/skills",
    includePrompt ? { query: { include_prompt: true } } : undefined,
  )
}

/**
 * GET /api/skills/{name}——单条详情。
 *
 * includePrompt=true 默认 403；只有 server 启用 allow_prompt_preview=True
 * 且请求来自 localhost 才返回 prompt body。普通用户场景保持 false。
 */
export function getSkill(name: string, includePrompt = false) {
  return requestJson<SkillDetailResponse>(
    `/api/skills/${encodeURIComponent(name)}`,
    includePrompt ? { query: { include_prompt: true } } : undefined,
  )
}

/**
 * POST /api/skills/upload——multipart 上传 SKILL.md。
 *
 * - 字段名 `files` 可重复
 * - 重名返回 409（含 skill_name）
 * - 格式错（frontmatter / utf-8）返回 400
 */
export function uploadSkill(file: File | File[] | FileList) {
  const formData = new FormData()
  const arr = Array.from(file as FileList | File[])
  for (const f of arr) {
    formData.append("files", f, f.name)
  }
  return uploadForm<SkillUploadResponse>("/api/skills/upload", formData)
}

/** POST /api/skills/{name}/enable。 */
export function enableSkill(name: string) {
  return requestJson<SkillToggleResponse>(
    `/api/skills/${encodeURIComponent(name)}/enable`,
    { method: "POST" },
  )
}

/** POST /api/skills/{name}/disable。 */
export function disableSkill(name: string) {
  return requestJson<SkillToggleResponse>(
    `/api/skills/${encodeURIComponent(name)}/disable`,
    { method: "POST" },
  )
}
