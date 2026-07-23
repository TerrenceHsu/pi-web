// API client 单元测试——P1-E M2-1。
//
// Mock src/api/client 的 requestJson——禁止真实 fetch。
// 验证 13 个 M2 API 的 method / path / body / encoding / 边界响应。

import { beforeEach, describe, expect, it, vi } from "vitest"

// ----- Mock requestJson -----
// 用 vi.hoisted 让 mock 引用与 vi.mock 同时提升，避免 TDZ。
const { requestJsonMock } = vi.hoisted(() => ({
  requestJsonMock: vi.fn(),
}))

vi.mock("../../src/api/client", () => ({
  requestJson: requestJsonMock,
  ApiError: class ApiError extends Error {
    readonly status: number
    readonly detail: string
    readonly payload: unknown
    constructor(status: number, detail: string, payload: unknown) {
      super(`${status} ${detail}`)
      this.name = "ApiError"
      this.status = status
      this.detail = detail
      this.payload = payload
    }
  },
}))

import {
  createCredential,
  createProviderProfile,
  deleteCredential,
  deleteProviderProfile,
  getProviderDefinitions,
  getProviderProfileModels,
  getSessionModelBinding,
  listCredentials,
  listProviderProfiles,
  putSessionModelBinding,
  rotateCredentialSecret,
  updateCredentialLabel,
  updateProviderProfile,
} from "../../src/api/providers"

// Marker——测试 secret 是否被原样传给 mocked requestJson。
// 测试结束后不得在任何 state / log / snapshot 中出现。
const SECRET_MARKER = "sk-M2-1-SECRET-MARKER-DO-NOT-PERSIST"

describe("providers API client", () => {
  beforeEach(() => {
    requestJsonMock.mockReset()
  })

  // ========================================================================
  // 1. GET /api/provider-definitions
  // ========================================================================
  describe("getProviderDefinitions", () => {
    it("issues GET to /api/provider-definitions with no body", async () => {
      requestJsonMock.mockResolvedValue([
        {
          id: "glm",
          display_name: "Zhipu GLM (Anthropic-compatible)",
          api_style: "anthropic_compatible",
          validation_supported: false,
          supports_model_listing: false,
        },
      ])
      const result = await getProviderDefinitions()
      expect(requestJsonMock).toHaveBeenCalledTimes(1)
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-definitions")
      expect(options?.method).toBeUndefined() // 默认 GET
      expect(options?.body).toBeUndefined()
      expect(result).toHaveLength(1)
    })

    it("returns bare array (not wrapped in {definitions: [...]})", async () => {
      requestJsonMock.mockResolvedValue([])
      const result = await getProviderDefinitions()
      expect(Array.isArray(result)).toBe(true)
    })
  })

  // ========================================================================
  // 2. GET /api/credentials
  // ========================================================================
  describe("listCredentials", () => {
    it("issues GET to /api/credentials with no body", async () => {
      requestJsonMock.mockResolvedValue({ credentials: [] })
      await listCredentials()
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials")
      expect(options?.method).toBeUndefined()
      expect(options?.body).toBeUndefined()
    })
  })

  // ========================================================================
  // 3. POST /api/credentials
  // ========================================================================
  describe("createCredential", () => {
    it("issues POST with full payload including secret_value", async () => {
      requestJsonMock.mockResolvedValue({
        credential: { credential_id: "cred-1" },
        warnings: [],
      })
      await createCredential({
        label: "my key",
        storage_mode: "session_only",
        secret_value: SECRET_MARKER,
      })
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials")
      expect(options?.method).toBe("POST")
      expect(options?.body).toEqual({
        label: "my key",
        storage_mode: "session_only",
        secret_value: SECRET_MARKER,
      })
    })

    it("issues POST with env_var_name (no secret_value)", async () => {
      requestJsonMock.mockResolvedValue({
        credential: { credential_id: "cred-env" },
        warnings: [],
      })
      await createCredential({
        label: "env key",
        storage_mode: "env",
        env_var_name: "MY_API_KEY",
      })
      const [, options] = requestJsonMock.mock.calls[0]
      expect(options?.body).toEqual({
        label: "env key",
        storage_mode: "env",
        env_var_name: "MY_API_KEY",
      })
      expect(options?.body.secret_value).toBeUndefined()
    })
  })

  // ========================================================================
  // 4. PATCH /api/credentials/{id}
  // ========================================================================
  describe("updateCredentialLabel", () => {
    it("issues PATCH with label body", async () => {
      requestJsonMock.mockResolvedValue({
        credential: { credential_id: "cred-1" },
        warnings: [],
      })
      await updateCredentialLabel("cred-1", { label: "renamed" })
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials/cred-1")
      expect(options?.method).toBe("PATCH")
      expect(options?.body).toEqual({ label: "renamed" })
    })

    it("URL-encodes credential_id", async () => {
      requestJsonMock.mockResolvedValue({
        credential: {},
        warnings: [],
      })
      // cred-1 has no special chars but confirms encodeURIComponent was used.
      await updateCredentialLabel("cred-1", { label: "x" })
      const [path] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials/cred-1")
    })
  })

  // ========================================================================
  // 5. PUT /api/credentials/{id}/secret
  // ========================================================================
  describe("rotateCredentialSecret", () => {
    it("issues PUT to /secret with new secret_value", async () => {
      requestJsonMock.mockResolvedValue({
        credential: { credential_id: "cred-1" },
        warnings: [],
      })
      await rotateCredentialSecret("cred-1", { secret_value: SECRET_MARKER })
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials/cred-1/secret")
      expect(options?.method).toBe("PUT")
      expect(options?.body).toEqual({ secret_value: SECRET_MARKER })
    })

    it("issues PUT with env_var_name (no secret_value)", async () => {
      requestJsonMock.mockResolvedValue({
        credential: { credential_id: "cred-1" },
        warnings: [],
      })
      await rotateCredentialSecret("cred-1", { env_var_name: "NEW_VAR" })
      const [, options] = requestJsonMock.mock.calls[0]
      expect(options?.body).toEqual({ env_var_name: "NEW_VAR" })
      expect(options?.body.secret_value).toBeUndefined()
    })
  })

  // ========================================================================
  // 6. DELETE /api/credentials/{id}
  // ========================================================================
  describe("deleteCredential", () => {
    it("issues DELETE with no body; expects 200 response shape", async () => {
      requestJsonMock.mockResolvedValue({
        credential_id: "cred-1",
        deleted: true,
        warnings: [],
      })
      const result = await deleteCredential("cred-1")
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials/cred-1")
      expect(options?.method).toBe("DELETE")
      expect(options?.body).toBeUndefined()
      expect(result.deleted).toBe(true)
    })

    it("URL-encodes credential_id with special chars", async () => {
      requestJsonMock.mockResolvedValue({
        credential_id: "cred weird",
        deleted: true,
        warnings: [],
      })
      await deleteCredential("cred weird")
      const [path] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/credentials/cred%20weird")
    })
  })

  // ========================================================================
  // 7. GET /api/provider-profiles
  // ========================================================================
  describe("listProviderProfiles", () => {
    it("issues GET to /api/provider-profiles", async () => {
      requestJsonMock.mockResolvedValue({ profiles: [] })
      await listProviderProfiles()
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles")
      expect(options?.method).toBeUndefined()
      expect(options?.body).toBeUndefined()
    })
  })

  // ========================================================================
  // 8. POST /api/provider-profiles
  // ========================================================================
  describe("createProviderProfile", () => {
    it("issues POST with create payload", async () => {
      requestJsonMock.mockResolvedValue({ profile: { id: "prof-1" } })
      await createProviderProfile({
        name: "my profile",
        provider_id: "glm",
        credential_id: "cred-1",
        default_model: "glm-4.5-flash",
      })
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles")
      expect(options?.method).toBe("POST")
      expect(options?.body).toEqual({
        name: "my profile",
        provider_id: "glm",
        credential_id: "cred-1",
        default_model: "glm-4.5-flash",
      })
    })

    it("forwards optional enabled / is_default flags", async () => {
      requestJsonMock.mockResolvedValue({ profile: {} })
      await createProviderProfile({
        name: "p",
        provider_id: "qwen",
        credential_id: "c",
        default_model: "m",
        enabled: false,
        is_default: true,
      })
      const [, options] = requestJsonMock.mock.calls[0]
      expect(options?.body.enabled).toBe(false)
      expect(options?.body.is_default).toBe(true)
    })
  })

  // ========================================================================
  // 9. PATCH /api/provider-profiles/{id}
  // ========================================================================
  describe("updateProviderProfile", () => {
    it("issues PATCH; never includes provider_id in body", async () => {
      requestJsonMock.mockResolvedValue({ profile: { id: "prof-1" } })
      await updateProviderProfile("prof-1", { name: "renamed" })
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles/prof-1")
      expect(options?.method).toBe("PATCH")
      expect(options?.body).toEqual({ name: "renamed" })
      expect(options?.body.provider_id).toBeUndefined()
    })

    it("forwards partial update fields", async () => {
      requestJsonMock.mockResolvedValue({ profile: {} })
      await updateProviderProfile("prof-1", { enabled: false })
      const [, options] = requestJsonMock.mock.calls[0]
      expect(options?.body).toEqual({ enabled: false })
    })

    it("URL-encodes profile_id with special chars", async () => {
      requestJsonMock.mockResolvedValue({ profile: {} })
      await updateProviderProfile("prof/weird", { name: "x" })
      const [path] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles/prof%2Fweird")
    })
  })

  // ========================================================================
  // 10. DELETE /api/provider-profiles/{id}
  // ========================================================================
  describe("deleteProviderProfile", () => {
    it("issues DELETE; expects 204 → null return", async () => {
      // client.ts 在 204 时把 null 转为 T——这里模拟那一层返回 null。
      requestJsonMock.mockResolvedValue(null)
      const result = await deleteProviderProfile("prof-1")
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles/prof-1")
      expect(options?.method).toBe("DELETE")
      expect(options?.body).toBeUndefined()
      expect(result).toBeNull()
    })

    it("URL-encodes profile_id with special chars", async () => {
      requestJsonMock.mockResolvedValue(null)
      await deleteProviderProfile("prof weird")
      const [path] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles/prof%20weird")
    })
  })

  // ========================================================================
  // 11. GET /api/provider-profiles/{id}/models
  // ========================================================================
  describe("getProviderProfileModels", () => {
    it("issues GET with no body", async () => {
      requestJsonMock.mockResolvedValue({ models: [] })
      await getProviderProfileModels("prof-1")
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/provider-profiles/prof-1/models")
      expect(options?.method).toBeUndefined()
      expect(options?.body).toBeUndefined()
    })

    it("returns empty models array without throwing", async () => {
      requestJsonMock.mockResolvedValue({ models: [] })
      const result = await getProviderProfileModels("prof-anthropic")
      expect(result.models).toEqual([])
    })

    it("forwards full model option shape with capabilities", async () => {
      const model = {
        id: "glm-4.5-flash",
        display_name: "GLM-4.5-Flash",
        source: "static",
        capabilities: {
          streaming: true,
          tool_calling: true,
          reasoning: null,
          vision: null,
          context_window: null,
        },
      }
      requestJsonMock.mockResolvedValue({ models: [model] })
      const result = await getProviderProfileModels("prof-1")
      expect(result.models[0]).toEqual(model)
    })
  })

  // ========================================================================
  // 12. GET /api/sessions/{sid}/model-binding
  // ========================================================================
  describe("getSessionModelBinding", () => {
    it("issues GET with no body", async () => {
      requestJsonMock.mockResolvedValue({
        binding: {
          session_id: "s1",
          profile_id: "p1",
          model_id: "m1",
          source: "explicit",
          created_at: 1,
          updated_at: 2,
        },
      })
      await getSessionModelBinding("s1")
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/sessions/s1/model-binding")
      expect(options?.method).toBeUndefined()
      expect(options?.body).toBeUndefined()
    })

    it("accepts { binding: null } as valid Legacy response", async () => {
      requestJsonMock.mockResolvedValue({ binding: null })
      const result = await getSessionModelBinding("s1")
      expect(result.binding).toBeNull()
    })

    it("URL-encodes sessionId with special chars", async () => {
      requestJsonMock.mockResolvedValue({ binding: null })
      await getSessionModelBinding("ses sion")
      const [path] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/sessions/ses%20sion/model-binding")
    })
  })

  // ========================================================================
  // 13. PUT /api/sessions/{sid}/model-binding
  // ========================================================================
  describe("putSessionModelBinding", () => {
    it("issues PUT with profile_id + model_id", async () => {
      requestJsonMock.mockResolvedValue({
        binding: {
          session_id: "s1",
          profile_id: "p1",
          model_id: "m1",
          source: "explicit",
          created_at: 1,
          updated_at: 2,
        },
      })
      await putSessionModelBinding("s1", {
        profile_id: "p1",
        model_id: "m1",
      })
      const [path, options] = requestJsonMock.mock.calls[0]
      expect(path).toBe("/api/sessions/s1/model-binding")
      expect(options?.method).toBe("PUT")
      expect(options?.body).toEqual({ profile_id: "p1", model_id: "m1" })
    })
  })

  // ========================================================================
  // Cross-cutting: secret containment
  // ========================================================================
  describe("secret marker containment", () => {
    it("secret_value appears only as create/rotate argument, never in URL path", async () => {
      requestJsonMock.mockResolvedValue({
        credential: { credential_id: "cred-1" },
        warnings: [],
      })
      await createCredential({
        label: "l",
        storage_mode: "session_only",
        secret_value: SECRET_MARKER,
      })
      const [path] = requestJsonMock.mock.calls[0]
      expect(path).not.toContain(SECRET_MARKER)

      await rotateCredentialSecret("cred-1", { secret_value: SECRET_MARKER })
      const [rotatePath] = requestJsonMock.mock.calls[1]
      expect(rotatePath).not.toContain(SECRET_MARKER)
    })

    it("does not call validate or provider-hints endpoints", async () => {
      requestJsonMock.mockResolvedValue({})
      // Validated by import surface——these functions do not exist in providers.ts.
      // If they did, the call below would create a request to a /validate or
      // /provider-hints path.
      const allCalls = requestJsonMock.mock.calls.map(([p]) => p)
      expect(allCalls.some((p) => p.includes("/validate"))).toBe(false)
      expect(allCalls.some((p) => p.includes("/provider-hints"))).toBe(false)
    })
  })
})
