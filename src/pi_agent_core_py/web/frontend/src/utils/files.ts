// File-related utilities——P0-2/P0-3 与后端 format 同步。
//
// 后端 src/pi_agent_core_py/tools/view_file.py 的 _classify_format 把文件
// 分成 markdown / html / csv / parquet / text / pdf / image_unsupported /
// binary / unsupported。前端用同样的语义决定 FileChip 显示状态。

import type { FileFormat, FileRef } from "../types"

/** 把字节数转成人类可读的字符串。 */
export function formatFileSize(bytes: number | undefined | null): string {
  if (bytes === undefined || bytes === null || bytes <= 0) return ""
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/**
 * 根据文件名 + mime 推断 format——后端 FileRef.format 已带，但前端
 * 选完文件还没上传时也可以先 preview。
 */
export function inferFileFormat(
  name: string | undefined,
  mime?: string | undefined,
): FileFormat {
  const n = (name || "").toLowerCase()
  const m = (mime || "").toLowerCase()

  if (m.startsWith("image/")) return "image_unsupported"
  if (m === "application/pdf" || n.endsWith(".pdf")) return "pdf"
  if (n.endsWith(".md") || n.endsWith(".markdown") || m === "text/markdown") {
    return "markdown"
  }
  if (n.endsWith(".html") || n.endsWith(".htm") || m === "text/html") {
    return "html"
  }
  if (n.endsWith(".csv") || m === "text/csv") return "csv"
  if (n.endsWith(".parquet")) return "parquet"
  // text/* 兜底（含 .txt / .json / .yaml / .py / .ts / .js / .sql 等）
  if (m.startsWith("text/") || isTextLikeExt(n)) return "text"
  return "unsupported"
}

const TEXT_LIKE_EXT = new Set([
  ".txt", ".json", ".yaml", ".yml", ".xml", ".toml",
  ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs",
  ".sql", ".sh", ".bat", ".ps1", ".ini", ".conf", ".log",
  ".md", ".rst", ".csv",
])

function isTextLikeExt(lowerName: string): boolean {
  for (const ext of TEXT_LIKE_EXT) {
    if (lowerName.endsWith(ext)) return true
  }
  return false
}

/** format 是否是 LLM 可读的支持格式。 */
export function isSupported(format: FileFormat | string | undefined): boolean {
  if (!format) return false
  return (
    format === "markdown" ||
    format === "html" ||
    format === "csv" ||
    format === "parquet" ||
    format === "text"
  )
}

export function isImage(format: FileFormat | string | undefined): boolean {
  return format === "image_unsupported"
}

export function isPdf(format: FileFormat | string | undefined): boolean {
  return format === "pdf"
}

/**
 * 给 FileChip 显示的简短支持标签。
 *
 * - supported → "supported"
 * - pdf → "not parsed"
 * - image / binary / unsupported → "unsupported"
 */
export function fileSupportLabel(format: FileFormat | string | undefined): string {
  if (!format) return "unknown"
  if (isSupported(format)) return "supported"
  if (isPdf(format)) return "not parsed"
  if (isImage(format)) return "image unsupported"
  return "unsupported"
}

/**
 * 给用户消息的简短 hint——MD/HTML/CSV/parquet/text 都可以读，图片不支持。
 */
export function fileSupportHint(format: FileFormat | string | undefined): string {
  if (!format) return ""
  if (isImage(format)) return "当前不支持图片内容分析（不做 OCR / 视觉理解）"
  if (isPdf(format)) return "PDF 正文暂未解析（仅元信息可见）"
  if (isSupported(format)) return "已附加：模型可用 view_file / list_files 读取"
  return "二进制 / 未知格式：仅元信息可见"
}

/** 推断 FileRef 的 format——后端没返回 format 时用此兜底。 */
export function refFormat(ref: FileRef): FileFormat {
  if (ref.format) return ref.format
  return inferFileFormat(ref.name, ref.mime)
}
