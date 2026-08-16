import MarkdownIt from "markdown-it"

const markdown = new MarkdownIt({
  // LLM 输出是不可信输入：保留 Markdown，但不执行其中的原始 HTML。
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
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
