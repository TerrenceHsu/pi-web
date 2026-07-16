// Skills 类型（Step 14 + P0-4 Step 1 上传 / enable / disable API）。

import type { JsonValue } from "./state"

/** Skill 摘要——GET /api/skills 列表 + POST /api/skills/upload 后单条。 */
export interface SkillSummary {
  name: string
  description: string
  status: "enabled" | "disabled" | string
  priority: number
  tags: string[]
  tool_names: string[]
  metadata: JsonValue
  /** 仅 GET ?include_prompt=true 时返回——默认不暴露。 */
  prompt?: JsonValue
}

/** Skill 详情——GET /api/skills/{name}。结构与 SkillSummary 一致；保留独立类型便于演进。 */
export type SkillDetail = SkillSummary

/** GET /api/skills response。 */
export interface SkillListResponse {
  attached: boolean
  skills: SkillSummary[]
  skill_loader?: JsonValue
}

/** GET /api/skills/{name}——单条 skill 详情。 */
export type SkillDetailResponse = SkillDetail

/**
 * POST /api/skills/upload response。
 *
 * 全部成功 200；部分失败 207；全失败 4xx（含 409 重名）。
 */
export interface SkillUploadResponse {
  count: number
  skills: SkillSummary[]
  errors?: Array<{
    filename: string
    skill_name?: string
    error_type: string
    error: string
    status?: number
  }>
}

/** POST /api/skills/{name}/enable | disable response。 */
export interface SkillToggleResponse {
  ok: boolean
  name: string
  status: "enabled" | "disabled" | string
}
