import type { AboutLicensesResponse } from "../types/about"
import { downloadBlob, requestBlob, requestJson } from "./client"

export function getAboutLicenses(): Promise<AboutLicensesResponse> {
  return requestJson<AboutLicensesResponse>("/api/about/licenses")
}

export async function downloadWorkerSource(
  sourceArchiveUrl: string,
  fallbackFilename: string,
): Promise<void> {
  const { blob, filename } = await requestBlob(sourceArchiveUrl)
  downloadBlob(blob, filename || fallbackFilename)
}
