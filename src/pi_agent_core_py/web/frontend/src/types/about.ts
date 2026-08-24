export interface ApplicationLicenseView {
  name: string
  version: string
  license_expression: string
}

export interface OpenSourceComponentView {
  component_id: string
  name: string
  version: string
  license_expression: string
  runtime_ready: boolean
  source_offer_available: boolean
  source_offer_url: string
  source_archive_url: string
  license_url: string
  notices_url: string
  sbom_url: string
  source_tree_sha256?: string
}

export interface AboutLicensesResponse {
  application: ApplicationLicenseView
  components: OpenSourceComponentView[]
  legal_notice: string
}
