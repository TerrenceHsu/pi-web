export type PythonTokenKind =
  | "plain"
  | "keyword"
  | "literal"
  | "builtin"
  | "function"
  | "class"
  | "self"
  | "number"
  | "string"
  | "comment"
  | "decorator"
  | "operator"

export interface PythonToken {
  kind: PythonTokenKind
  text: string
}

const KEYWORDS = new Set(
  "and as assert async await break case class continue def del elif else except finally for from global if import in is lambda match nonlocal not or pass raise return try while with yield".split(
    " ",
  ),
)
const LITERALS = new Set(["True", "False", "None", "NotImplemented", "Ellipsis"])
const BUILTINS = new Set(
  "abs all any bool bytes callable chr classmethod compile complex dict dir divmod enumerate eval exec filter float format frozenset getattr globals hasattr hash help hex id input int isinstance issubclass iter len list locals map max memoryview min next object oct open ord pow print property range repr reversed round set setattr slice sorted staticmethod str sum super tuple type vars zip __import__".split(
    " ",
  ),
)
const STRING_PREFIXES = new Set(["r", "u", "b", "f", "br", "rb", "fr", "rf"])

function append(tokens: PythonToken[], kind: PythonTokenKind, text: string): void {
  if (!text) return
  const previous = tokens.at(-1)
  if (previous?.kind === kind) previous.text += text
  else tokens.push({ kind, text })
}

function stringStart(source: string, index: number): { quoteAt: number; quote: string } | null {
  for (const prefixLength of [2, 1, 0]) {
    const quoteAt = index + prefixLength
    const quote = source[quoteAt]
    if (quote !== '"' && quote !== "'") continue
    const prefix = source.slice(index, quoteAt).toLocaleLowerCase()
    if (prefixLength === 0 || STRING_PREFIXES.has(prefix)) return { quoteAt, quote }
  }
  return null
}

function stringEnd(source: string, start: number, quoteAt: number, quote: string): number {
  const triple = source.slice(quoteAt, quoteAt + 3) === quote.repeat(3)
  const delimiter = triple ? quote.repeat(3) : quote
  let cursor = quoteAt + delimiter.length
  while (cursor < source.length) {
    if (source[cursor] === "\\") {
      cursor += 2
      continue
    }
    if (source.startsWith(delimiter, cursor)) return cursor + delimiter.length
    if (!triple && (source[cursor] === "\n" || source[cursor] === "\r")) return cursor
    cursor += 1
  }
  return Math.max(cursor, start + 1)
}

/** Small, dependency-free lexer for safe, read-only Python source previews. */
export function highlightPython(source: string): PythonToken[] {
  const tokens: PythonToken[] = []
  let index = 0
  let declaration: "function" | "class" | null = null

  while (index < source.length) {
    const rest = source.slice(index)
    const whitespace = /^\s+/.exec(rest)?.[0]
    if (whitespace) {
      append(tokens, "plain", whitespace)
      index += whitespace.length
      continue
    }
    if (source[index] === "#") {
      const end = source.indexOf("\n", index)
      const value = source.slice(index, end < 0 ? source.length : end)
      append(tokens, "comment", value)
      index += value.length
      continue
    }
    const start = stringStart(source, index)
    if (start) {
      const end = stringEnd(source, index, start.quoteAt, start.quote)
      append(tokens, "string", source.slice(index, end))
      index = end
      continue
    }
    if (source[index] === "@") {
      const decorator = /^@[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/.exec(rest)?.[0]
      if (decorator) {
        append(tokens, "decorator", decorator)
        index += decorator.length
        continue
      }
    }
    const number =
      /^(?:0[xX][\dA-Fa-f](?:_?[\dA-Fa-f])*|0[bB][01](?:_?[01])*|0[oO][0-7](?:_?[0-7])*|(?:\d(?:_?\d)*)?(?:\.\d(?:_?\d)*)?(?:[eE][+-]?\d(?:_?\d)*)?j?)/.exec(
        rest,
      )?.[0]
    if (number && /\d/.test(number)) {
      append(tokens, "number", number)
      index += number.length
      continue
    }
    const identifier = /^[A-Za-z_]\w*/.exec(rest)?.[0]
    if (identifier) {
      let kind: PythonTokenKind = "plain"
      if (declaration) {
        kind = declaration
        declaration = null
      } else if (KEYWORDS.has(identifier)) {
        kind = "keyword"
        if (identifier === "def") declaration = "function"
        if (identifier === "class") declaration = "class"
      } else if (LITERALS.has(identifier)) kind = "literal"
      else if (BUILTINS.has(identifier)) kind = "builtin"
      else if (identifier === "self" || identifier === "cls") kind = "self"
      append(tokens, kind, identifier)
      index += identifier.length
      continue
    }
    const operator = /^[+\-*/%@<>=!&|^~:.,;()[\]{}]+/.exec(rest)?.[0]
    if (operator) {
      append(tokens, "operator", operator)
      index += operator.length
      continue
    }
    append(tokens, "plain", source[index] ?? "")
    index += 1
  }
  return tokens
}
