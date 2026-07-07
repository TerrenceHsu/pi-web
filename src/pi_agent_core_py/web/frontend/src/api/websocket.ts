// WebSocket client——/ws/events 自动重连。
//
// - 自动 ws/wss 转换（基于 location.protocol）
// - 默认 host = location.host；path = /ws/events
// - 自动重连，退避 [1s, 2s, 4s, 5s, 5s]——上限 5s
// - hello / shutdown 等事件原样传给 onEvent，由 chatStore 决定如何展示
//
// 不在这里做 chat 逻辑——保持纯通道。

export interface EventSocketOptions {
  /** 收到任意事件（含 hello / shutdown / 业务事件） */
  onEvent?: (event: any) => void
  /** 连接成功（含重连） */
  onOpen?: () => void
  /** 连接关闭（含主动 close / 服务端关闭 / 网络断开） */
  onClose?: (event: CloseEvent) => void
  /** WebSocket 错误——通常是网络层问题，会触发自动重连 */
  onError?: (event: Event) => void
  /** 重连开始——可显示 reconnecting 状态 */
  onReconnecting?: (attempt: number, delayMs: number) => void
  /** 自定义 URL；不传则用 location.host + /ws/events */
  url?: string
  /** 最大重连退避（默认 5000ms） */
  maxBackoffMs?: number
}

export interface EventSocket {
  /** 主动关闭——不再自动重连 */
  close: () => void
  /** 手动触发重连（不依赖 close 事件） */
  reconnect: () => void
  /** 当前是否连接中 */
  isOpen: () => boolean
}

/**
 * 创建一个 EventSocket。
 *
 * 调用方负责在 unmount 时 close()，避免泄漏。
 */
export function createEventSocket(opts: EventSocketOptions): EventSocket {
  const url = opts.url ?? defaultUrl()
  const maxBackoff = opts.maxBackoffMs ?? 5000

  let ws: WebSocket | null = null
  let closedByUser = false
  let reconnectAttempt = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  function connect() {
    if (closedByUser) return
    ws = new WebSocket(url)

    ws.onopen = () => {
      reconnectAttempt = 0
      opts.onOpen?.()
    }

    ws.onmessage = (e: MessageEvent) => {
      // 后端发的是 JSON 文本——try parse
      let data: any = e.data
      if (typeof data === "string") {
        try {
          data = JSON.parse(data)
        } catch {
          // 非 JSON——保留原始字符串
        }
      }
      opts.onEvent?.(data)
    }

    ws.onerror = (event: Event) => {
      opts.onError?.(event)
    }

    ws.onclose = (event: CloseEvent) => {
      opts.onClose?.(event)
      ws = null
      if (!closedByUser) {
        scheduleReconnect()
      }
    }
  }

  function scheduleReconnect() {
    if (closedByUser) return
    if (reconnectTimer !== null) return

    reconnectAttempt += 1
    // 退避：1, 2, 4, 8... 截到 maxBackoff
    const raw = Math.pow(2, reconnectAttempt - 1) * 1000
    const delay = Math.min(raw, maxBackoff)
    opts.onReconnecting?.(reconnectAttempt, delay)

    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, delay)
  }

  // 首次连接——createEventSocket 调用时立即建立。
  // 之前遗漏：connect() 只定义不调用，导致 socket 创建后永远不连。
  connect()

  return {
    close() {
      closedByUser = true
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
      if (ws !== null) {
        try {
          ws.close()
        } catch {
          // 忽略——已经关闭
        }
        ws = null
      }
    },
    reconnect() {
      if (ws !== null) {
        try {
          ws.close()
        } catch {
          // 忽略
        }
        ws = null
      }
      // 重置 closedByUser——手动 reconnect 是用户期望继续连
      closedByUser = false
      connect()
    },
    isOpen() {
      return ws !== null && ws.readyState === WebSocket.OPEN
    },
  }
}

function defaultUrl(): string {
  if (typeof window === "undefined" || !window.location) {
    // SSR / 测试环境——返回一个 placeholder，调用方应传 url
    return "ws://localhost:8000/ws/events"
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${proto}//${window.location.host}/ws/events`
}
