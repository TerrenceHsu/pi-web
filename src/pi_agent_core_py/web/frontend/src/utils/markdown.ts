import MarkdownIt from "markdown-it"
import { tex } from "@mdit/plugin-tex"
import katex from "katex"

const markdown = new MarkdownIt({
  // LLM 输出是不可信输入：保留 Markdown，但不执行其中的原始 HTML。
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
})

// Parse math before Markdown escaping/emphasis, never inside ordinary code fences.
// Both formula text and model output are untrusted; no HTML/URL commands or shared macros.
markdown.use(tex, {
  delimiters: "all",
  mathFence: false,
  allowInlineWithSpace: false,
  render: (content: string, displayMode: boolean) => {
    const kind = displayMode ? "math-display" : "math-inline"
    try {
      if (content.length > 16384) throw new Error("Formula too long")
      return `<span class="${kind}">${katex.renderToString(content, {
        displayMode,
        output: "htmlAndMathml",
        trust: false,
        strict: "ignore",
        throwOnError: true,
        maxExpand: 1000,
        maxSize: 20,
        macros: {},
      })}</span>`
    } catch {
      // Incomplete streamed/unsupported formulas remain readable, with no raw
      // parser errors or unescaped source inserted into the DOM.
      return `<span class="${kind} math-source">${markdown.utils.escapeHtml(content)}</span>`
    }
  },
})

const defaultLinkOpen = markdown.renderer.rules.link_open

markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  const token = tokens[index]
  token.attrSet("target", "_blank")
  token.attrSet("rel", "noopener noreferrer")

  if (defaultLinkOpen) {
    return defaultLinkOpen(tokens, index, options, env, renderer)
  }
  return renderer.renderToken(tokens, index, options)
}

/** 将不可信的 LLM 文本转换成可展示的安全 Markdown HTML。 */
export function renderMarkdown(source: string): string {
  return markdown.render(source)
}
