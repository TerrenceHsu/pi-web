import { describe, expect, it } from "vitest"
import { renderMarkdown } from "../../src/utils/markdown"
import { PPO_MATH } from "../fixtures/math"

function rendered(source: string) {
  const root = document.createElement("div")
  root.innerHTML = renderMarkdown(source)
  return root
}

describe("safe mathematical Markdown", () => {
  it("renders the PPO example with fractions, subscripts and bold inline advantages", () => {
    const root = rendered(PPO_MATH)
    expect(root.querySelectorAll(".katex-display")).toHaveLength(2)
    expect(root.querySelectorAll(".math-inline")).toHaveLength(3)
    expect(root.querySelector("mfrac")).not.toBeNull()
    expect(root.querySelector("msub")).not.toBeNull()
    expect(root.querySelector("strong .katex")).not.toBeNull()
    expect(root.querySelector(".math-source")).toBeNull()
  })

  it.each([String.raw`$x_t$`, String.raw`\(x_t\)`])("renders inline %s", (source) => {
    expect(rendered(source).querySelector(".math-inline msub")).not.toBeNull()
  })

  it.each([String.raw`$$\frac{a}{b}$$`, "$$\n\\frac{a}{b}\n$$", String.raw`\[\frac{a}{b}\]`])(
    "renders display %s",
    (source) => {
      expect(rendered(source).querySelector(".math-display mfrac")).not.toBeNull()
    },
  )

  it("preserves code fences, inline code, escaped dollars and prices", () => {
    const root = rendered("`$x_t$`\n\n```tex\n$$x_t$$\n```\n\nCost $5 and $10.\n\n\\$x$.")
    expect(root.querySelector(".katex")).toBeNull()
    expect(root.querySelector("pre code")?.textContent).toContain("$$x_t$$")
    expect(root.textContent).toContain("Cost $5 and $10")
  })

  it("does not rewrite a literal escaped underscore into a different formula", () => {
    const root = rendered(String.raw`$x\_t$`)
    expect(root.querySelector(".katex")).not.toBeNull()
    expect(root.querySelector("msub")).toBeNull()
    expect(root.querySelector("annotation")?.textContent).toBe(String.raw`x\_t`)
  })

  it("handles incomplete streamed math and invalid TeX without crashing or HTML injection", () => {
    expect(() => renderMarkdown(String.raw`Result: $\frac{a`)).not.toThrow()
    const root = rendered(String.raw`$$\badcommand{<img src=x onerror=alert(1)>}$$`)
    expect(root.querySelector(".math-source")?.textContent).toContain("<img")
    expect(root.querySelector("img")).toBeNull()
  })

  it("blocks unsafe math commands and ordinary raw HTML", () => {
    const root = rendered(String.raw`$\href{javascript:alert(1)}{click}$

$\includegraphics{https://example.com/tracker}$

$\htmlClass{injected}{x}$

<script>alert(1)</script>`)
    expect(root.querySelector("a, img, script, .injected")).toBeNull()
  })

  it("bounds recursive macros, large formulas and isolates macro definitions", () => {
    expect(
      rendered(String.raw`$\def\loop{\loop}\loop$`).querySelector(".math-source"),
    ).not.toBeNull()
    expect(rendered(`$$${"x".repeat(16385)}$$`).querySelector(".math-source")).not.toBeNull()
    renderMarkdown(String.raw`$\gdef\secret{42}$`)
    expect(rendered(String.raw`$\secret$`).querySelector(".math-source")).not.toBeNull()
  })
})
