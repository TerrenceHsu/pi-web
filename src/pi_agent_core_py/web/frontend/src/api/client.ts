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
