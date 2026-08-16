// 统一 fetch 封装——所有 api/*.ts 模块共用。
//
// - 2xx 返回 JSON；204 返回 null
// - 非 2xx 抛 ApiError，含 status / detail / payload
// - detail 优先从 response JSON 的 detail 字段读
// - P1-E M2-F1：所有同源 UI Backend 请求强制注入 X-PI-Agent-UI header（caller 无法覆盖）
//
// 不在这里做 retry / cache——store 层决定如何处理错误。

/** API_BASE 默认空——前端同源 FastAPI 托管，不需要 baseURL。 */
export const API_BASE = ""

/**
 * P1-E M2-F1：Trusted UI Header。
 *
 * 后端 Credential / Provider Profile / Session Binding / Provider Definitions
 * 路由强制要求 `X-PI-Agent-UI: 1` header，否则返回 400 missing_ui_header。
 * 该 header 是 "前端来自我们自己的 UI" 的轻量标记——非 auth——但缺失会让所有
 * Provider 相关 endpoint 在产品环境永久失败。
 *
 * 约束（M2-F1 冻结）：
 * - 仅注入同源 UI Backend 请求（本文件三个 helper：requestJson / uploadForm / requestBlob）
 * - 调用方不能覆盖或删除——`createUiHeaders` 总是 `set` 该 header
 * - 调用方已有 header 保留
 * - 不用于外部 Provider URL（前端永远不直连外部 Provider）
 */
export const UI_HEADER_NAME = "X-PI-Agent-UI"
export const UI_HEADER_VALUE = "1"

/**
 * 构造 Headers——始终注入 UI Header，强制覆盖调用方传入了的同名 header。
 *
 * 用 Headers API（而非 plain object）以便调用方传入 HeadersInit 多形态
 * （Record / Headers / array）。
 */
function createUiHeaders(initial?: HeadersInit): Headers {
  const headers = new Headers(initial)
  headers.set(UI_HEADER_NAME, UI_HEADER_VALUE)
  return headers
}

/**
 * 把 backend 非 2xx 响应体转为安全可展示的 detail 字符串。
 *
 * 安全约束（M2-F1 冻结）：
 * - 优先 backend 已脱敏的 `message` 字段（FastAPI 中间件安全格式：`{error: {code, message}}`）
 * - 次选 `detail` 字符串（FastAPI HTTPException(detail=str) 模式）
 * - 次选 plain string payload
 * - **不**渲染 `detail` 数组（FastAPI validation 列表，可能含输入回显）
 * - **不**渲染 `error.code`（debug 字段，可能含内部信息）
 * - **不**用 `String(obj)` 兜底——会让对象变成 "[object Object]"
 * - 兜底用 statusText 或 fallback 固定文案
 */
function safeErrorDetail(payload: unknown, statusText: string, fallback: string): string {
  if (typeof payload === "string" && payload.length > 0) return payload
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>
    // 中间件安全格式：{error: {code, message}}
    const err = obj.error
    if (err && typeof err === "object") {
      const errMsg = (err as Record<string, unknown>).message
      if (typeof errMsg === "string" && errMsg.length > 0) return errMsg
    }
    // 顶层 message（部分自定义 handler 用此模式）
    if (typeof obj.message === "string" && obj.message.length > 0) return obj.message
    // FastAPI HTTPException(detail="...") 模式
    if (typeof obj.detail === "string" && obj.detail.length > 0) return obj.detail
    // 稳定领域错误：{detail: {code, message}}。只展示服务端脱敏 message。
    if (obj.detail && typeof obj.detail === "object") {
      const detailMessage = (obj.detail as Record<string, unknown>).message
      if (typeof detailMessage === "string" && detailMessage.length > 0) {
        return detailMessage
      }
    }
    // detail 数组（FastAPI validation 列表）——故意不渲染
  }
  if (typeof statusText === "string" && statusText.length > 0) return statusText
  return fallback
}

/** 统一错误类型——store 用 instanceof ApiError 区分网络错误 vs 业务错误。 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  readonly payload: any

  constructor(status: number, detail: string, payload: any) {
    super(`${status} ${detail}`)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
    this.payload = payload
  }
}

/**
 * 发送 JSON GET / POST / PATCH / DELETE 请求。
 *
 * - method 默认 GET
 * - body 不为 undefined / null 时按 JSON 序列化并加 Content-Type
 * - 204 No Content → 返回 null
 * - P1-E M2-F1：强制注入 X-PI-Agent-UI header（caller 无法覆盖）
 * - 非 2xx → 抛 ApiError（detail 由 safeErrorDetail 解析）
 */
export async function requestJson<T>(
  path: string,
  options: {
    method?: "GET" | "POST" | "PATCH" | "DELETE" | "PUT"
    body?: any
    query?: Record<string, string | number | boolean | undefined | null>
    signal?: AbortSignal
  } = {},
): Promise<T> {
  const url = buildUrl(path, options.query)
  const headers = createUiHeaders({ Accept: "application/json" })
  let bodyStr: string | undefined
  if (options.body !== undefined && options.body !== null) {
    headers.set("Content-Type", "application/json")
    bodyStr = JSON.stringify(options.body)
  }

  let resp: Response
  try {
    resp = await fetch(url, {
      method: options.method ?? "GET",
      headers,
      body: bodyStr,
      signal: options.signal,
    })
  } catch (e: any) {
    // 网络错误 / abort——抛 ApiError(status=0) 让 store 区分
    if (e?.name === "AbortError") throw e
    throw new ApiError(0, `network error: ${e?.message ?? e}`, null)
  }

  if (resp.status === 204) {
    return null as T
  }

  const text = await resp.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!resp.ok) {
    const detail = safeErrorDetail(payload, resp.statusText, "request failed")
    throw new ApiError(resp.status, detail, payload)
  }

  return payload as T
}

/**
 * 上传 multipart form data。
 *
 * - formData 由调用方构造（含字段名 files 等）
 * - 不设 Content-Type——浏览器自动加 boundary
 * - P1-E M2-F1：强制注入 X-PI-Agent-UI header；caller 无法覆盖
 * - 非 2xx → 抛 ApiError
 */
export async function uploadForm<T>(
  path: string,
  formData: FormData,
  options: {
    method?: "POST" | "PUT"
    signal?: AbortSignal
  } = {},
): Promise<T> {
  const url = API_BASE + path
  // 注意：不传 Content-Type——浏览器需要为 multipart 自动生成 boundary
  const headers = createUiHeaders({ Accept: "application/json" })
  let resp: Response
  try {
    resp = await fetch(url, {
      method: options.method ?? "POST",
      headers,
      body: formData,
      signal: options.signal,
    })
  } catch (e: any) {
    if (e?.name === "AbortError") throw e
    throw new ApiError(0, `network error: ${e?.message ?? e}`, null)
  }

  const text = await resp.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!resp.ok) {
    const detail = safeErrorDetail(payload, resp.statusText, "upload failed")
    throw new ApiError(resp.status, detail, payload)
  }

  return payload as T
}

/** 拼 URL + query string——跳过 undefined / null。 */
function buildUrl(path: string, query?: Record<string, any>): string {
  if (!query) return API_BASE + path
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue
    params.append(k, String(v))
  }
  const qs = params.toString()
  return API_BASE + path + (qs ? `?${qs}` : "")
}

/**
 * P1-D1: 下载 blob——用于 Export Markdown 等文件下载场景。
 *
 * - GET 请求，Accept: application/octet-stream
 * - 返回 { blob, filename }——filename 从 Content-Disposition 提取
 * - P1-E M2-F1：强制注入 X-PI-Agent-UI header
 * - 非 2xx → 抛 ApiError
 */
export async function requestBlob(path: string): Promise<{ blob: Blob; filename: string | null }> {
  const url = API_BASE + path
  const headers = createUiHeaders({ Accept: "*/*" })
  let resp: Response
  try {
    resp = await fetch(url, {
      method: "GET",
      headers,
    })
  } catch (e: any) {
    if (e?.name === "AbortError") throw e
    throw new ApiError(0, `network error: ${e?.message ?? e}`, null)
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => "")
    let payload: any = text
    try {
      payload = JSON.parse(text)
    } catch {
      // 非 JSON——保留原文
    }
    const detail = safeErrorDetail(payload, resp.statusText, "download failed")
    throw new ApiError(resp.status, detail, payload)
  }

  const blob = await resp.blob()
  // 从 Content-Disposition 提取 filename
  const cd = resp.headers.get("content-disposition") || ""
  let filename: string | null = null
  // RFC 5987 filename*=UTF-8''...
  const filenameStarMatch = cd.match(/filename\*=UTF-8''(.+?)(?:;|$)/i)
  if (filenameStarMatch) {
    try {
      filename = decodeURIComponent(filenameStarMatch[1])
    } catch {
      filename = filenameStarMatch[1]
    }
  } else {
    const filenameMatch = cd.match(/filename="?(.+?)"?(?:;|$)/i)
    if (filenameMatch) {
      filename = filenameMatch[1]
    }
  }
  return { blob, filename }
}

/**
 * P1-D1: 触发浏览器下载——从 blob + filename 创建临时 <a> 并 click。
 *
 * Blob URL 必须释放——多次导出不能积累。
 * 用 try/finally 保证 click 抛异常时 URL 仍被回收；
 * setTimeout(0) 推迟到下一个 macrotask，避免同步 revoke 中断 Safari 下载。
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement("a")
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
  } finally {
    // setTimeout(0)——让浏览器先把 download 任务派发出去
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}
