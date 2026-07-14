// 统一 fetch 封装——所有 api/*.ts 模块共用。
//
// - 2xx 返回 JSON；204 返回 null
// - 非 2xx 抛 ApiError，含 status / detail / payload
// - detail 优先从 response JSON 的 detail 字段读
//
// 不在这里做 retry / cache——store 层决定如何处理错误。

/** API_BASE 默认空——前端同源 FastAPI 托管，不需要 baseURL。 */
export const API_BASE = ""

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
 * - 非 2xx → 抛 ApiError（detail 从 body.detail 或 body.error 或 stringify）
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
  const headers: Record<string, string> = { Accept: "application/json" }
  let bodyStr: string | undefined
  if (options.body !== undefined && options.body !== null) {
    headers["Content-Type"] = "application/json"
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
    const detail =
      (payload && typeof payload === "object" && (payload.detail || payload.error)) ||
      (typeof payload === "string" && payload) ||
      resp.statusText ||
      `request failed`
    throw new ApiError(resp.status, String(detail), payload)
  }

  return payload as T
}

/**
 * 上传 multipart form data。
 *
 * - formData 由调用方构造（含字段名 files 等）
 * - 不设 Content-Type——浏览器自动加 boundary
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
  let resp: Response
  try {
    resp = await fetch(url, {
      method: options.method ?? "POST",
      headers: { Accept: "application/json" },
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
    const detail =
      (payload && typeof payload === "object" && (payload.detail || payload.error)) ||
      (typeof payload === "string" && payload) ||
      resp.statusText ||
      `upload failed`
    throw new ApiError(resp.status, String(detail), payload)
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
 * - 非 2xx → 抛 ApiError
 */
export async function requestBlob(
  path: string,
): Promise<{ blob: Blob; filename: string | null }> {
  const url = API_BASE + path
  let resp: Response
  try {
    resp = await fetch(url, {
      method: "GET",
      headers: { Accept: "*/*" },
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
    const detail =
      (payload && typeof payload === "object" && (payload.detail || payload.error)) ||
      resp.statusText ||
      "download failed"
    throw new ApiError(resp.status, String(detail), payload)
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
