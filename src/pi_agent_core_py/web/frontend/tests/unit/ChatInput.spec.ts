// ChatInput 单元测试——P1-E M2-3。
//
// 覆盖 spec §17 的 11 项测试：providerReady 门控、默认行为、Enter / submit 阻止、
// 附件阻止、abort/stop 不受影响、providerReady 不进入 payload。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { mount } from "@vue/test-utils"

import ChatInput from "../../src/components/chat/ChatInput.vue"

// ============================================================================
// Setup
// ============================================================================

beforeEach(() => {
  vi.clearAllMocks()
})

function mountInput(props: Partial<InstanceType<typeof ChatInput>["$props"]> = {}) {
  return mount(ChatInput, {
    props: {
      sending: false,
      sessionId: "sess-1",
      ...props,
    } as any,
  })
}

async function flushAll() {
  await new Promise((r) => setTimeout(r, 0))
}

// ============================================================================
// providerReady 门控
// ============================================================================

describe("providerReady gating", () => {
  it("defaults to true when not passed", async () => {
    const wrapper = mountInput()
    // 不传 providerReady 时默认 true——canSend 不被 providerReady 阻止
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    const sendBtn = wrapper.get('[data-testid="send-button"]')
    expect(sendBtn.attributes("disabled")).toBeUndefined()
  })

  it("providerReady=true + non-empty text → send enabled", async () => {
    const wrapper = mountInput({ providerReady: true })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    const sendBtn = wrapper.get('[data-testid="send-button"]')
    expect(sendBtn.attributes("disabled")).toBeUndefined()
  })

  it("providerReady=false + non-empty text → send disabled", async () => {
    const wrapper = mountInput({ providerReady: false })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    const sendBtn = wrapper.get('[data-testid="send-button"]')
    expect(sendBtn.attributes("disabled")).toBeDefined()
  })

  it("providerReady=false + pending attachment → send disabled", () => {
    const wrapper = mountInput({
      providerReady: false,
      pendingAttachments: [{ id: "f-1", name: "file.txt", size: 10 } as any],
    })
    const sendBtn = wrapper.get('[data-testid="send-button"]')
    expect(sendBtn.attributes("disabled")).toBeDefined()
  })

  it("providerReady=false + Enter does not emit send", async () => {
    const wrapper = mountInput({ providerReady: false })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    await wrapper
      .get('[data-testid="chat-input-field"]')
      .trigger("keydown", { key: "Enter", shiftKey: false, isComposing: false })
    expect(wrapper.emitted("submit")).toBeUndefined()
  })

  it("providerReady=false + direct submit() call does not emit", async () => {
    const wrapper = mountInput({ providerReady: false })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    // 调用组件实例的内部 submit
    ;(wrapper.vm as any).submit?.()
    await flushAll()
    expect(wrapper.emitted("submit")).toBeUndefined()
  })

  it("providerReady transitions false → true enables send", async () => {
    const wrapper = mountInput({ providerReady: false })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    expect(wrapper.get('[data-testid="send-button"]').attributes("disabled")).toBeDefined()
    await wrapper.setProps({ providerReady: true })
    expect(wrapper.get('[data-testid="send-button"]').attributes("disabled")).toBeUndefined()
  })

  it("Shift+Enter still inserts newline regardless of providerReady", async () => {
    const wrapper = mountInput({ providerReady: false })
    const field = wrapper.get('[data-testid="chat-input-field"]')
    await field.trigger("keydown", { key: "Enter", shiftKey: true, isComposing: false })
    expect(wrapper.emitted("submit")).toBeUndefined()
    // 没阻止默认——textarea 自然插入换行
  })

  it("sending state existing behavior unchanged", async () => {
    const wrapper = mountInput({ sending: true, providerReady: true })
    // sending 时 Stop button 出现
    expect(wrapper.find('[data-testid="stop-button"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="send-button"]').exists()).toBe(false)
  })

  it("stop/abort button unaffected by providerReady", async () => {
    const wrapper = mountInput({ sending: true, providerReady: false })
    const stopBtn = wrapper.get('[data-testid="stop-button"]')
    expect(stopBtn.attributes("disabled")).toBeUndefined()
    await stopBtn.trigger("click")
    expect(wrapper.emitted("abort")).toBeDefined()
  })

  it("providerReady does not enter send payload", async () => {
    const wrapper = mountInput({ providerReady: true })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("hello")
    await wrapper.get('[data-testid="send-button"]').trigger("click")
    const submitEvents = wrapper.emitted("submit")
    expect(submitEvents).toBeDefined()
    expect(submitEvents![0]).toEqual(["hello"])
    // payload 只是 text——不含 providerReady / provider 信息
  })
})

describe("Knowledge mode input", () => {
  it("removes Workspace attachments and explains the approval boundary", () => {
    const wrapper = mountInput({
      knowledgeMode: true,
      attachmentsEnabled: false,
      pendingAttachments: [{ id: "f-1", name: "secret.txt", size: 10 } as any],
    })

    expect(wrapper.find('[data-testid="attach-button"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="file-input"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="attachment-bar"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="chat-input-field"]').attributes("placeholder")).toContain(
      "Ask this Wiki",
    )
    expect(wrapper.text()).toContain("edits require approval")
  })
})

describe("slash command menu", () => {
  const commands = [
    {
      name: "/checkpointer",
      description: "Save memory and clear this conversation.",
      requires_provider: true,
      accepts_arguments: false,
    },
  ]

  it("shows matching commands when the user types slash", async () => {
    const wrapper = mountInput({ slashCommands: commands })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("/")
    expect(wrapper.find('[role="listbox"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="slash-command-checkpointer"]').text()).toContain(
      "/checkpointer",
    )
  })

  it("selects a command without sending it immediately", async () => {
    const wrapper = mountInput({ slashCommands: commands })
    await wrapper.get('[data-testid="chat-input-field"]').setValue("/check")
    await wrapper.get('[data-testid="slash-command-checkpointer"]').trigger("mousedown")
    expect((wrapper.get('[data-testid="chat-input-field"]').element as HTMLTextAreaElement).value)
      .toBe("/checkpointer")
    expect(wrapper.emitted("submit")).toBeUndefined()
  })

  it("submits an exact checkpointer command with Enter", async () => {
    const wrapper = mountInput({ slashCommands: commands })
    const field = wrapper.get('[data-testid="chat-input-field"]')
    await field.setValue("/checkpointer")
    await field.trigger("keydown", {
      key: "Enter",
      shiftKey: false,
      isComposing: false,
    })
    expect(wrapper.emitted("submit")?.[0]).toEqual(["/checkpointer"])
  })
})
