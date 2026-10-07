// Basic checks PineTS does not make (0.5.0, Owner 2026-10-06). PineTS runs a
// script with `a = close +`, a missing block indent or `ta.sma(close)` without
// complaint and returns quietly wrong signals (measured on 0.11.0), so the
// runner refuses those before running and says where. Not a parser: it catches
// the common slips, line and column 1-based, and nothing it reports is a
// matter of style. A script it passes can still fail in PineTS; locateError
// then points at the line where it can.

// Calls whose argument count is checked: [min, max] positional + named.
const ARITY = {
  'ta.sma': [2, 2], 'ta.ema': [2, 2], 'ta.rma': [2, 2], 'ta.wma': [2, 2], 'ta.hma': [2, 2], 'ta.vwma': [2, 2],
  'ta.rsi': [2, 2], 'ta.atr': [1, 1], 'ta.tr': [0, 1], 'ta.stdev': [2, 3], 'ta.variance': [2, 3],
  'ta.highest': [1, 2], 'ta.lowest': [1, 2], 'ta.highestbars': [1, 2], 'ta.lowestbars': [1, 2],
  'ta.crossover': [2, 2], 'ta.crossunder': [2, 2], 'ta.cross': [2, 2], 'ta.change': [1, 2],
  'ta.mom': [2, 2], 'ta.roc': [2, 2], 'ta.cci': [2, 2], 'ta.mfi': [2, 2], 'ta.percentrank': [2, 2],
  'ta.supertrend': [2, 2], 'ta.dmi': [2, 2], 'ta.macd': [4, 4], 'ta.bb': [3, 3], 'ta.kc': [3, 4],
  'ta.stoch': [4, 4], 'ta.linreg': [3, 3], 'ta.cum': [1, 1], 'ta.median': [2, 2], 'ta.falling': [2, 2],
  'ta.rising': [2, 2], 'ta.barssince': [1, 1], 'ta.valuewhen': [3, 3], 'ta.pivothigh': [2, 3], 'ta.pivotlow': [2, 3],
  'math.avg': [2, 99], 'math.max': [2, 99], 'math.min': [2, 99], 'math.abs': [1, 1], 'math.sqrt': [1, 1],
  'math.log': [1, 1], 'math.round': [1, 2], 'nz': [1, 2], 'na': [1, 1], 'fixnan': [1, 1],
}
const TRAILING_OPS = ['+', '-', '*', '/', '%', '==', '!=', '>=', '<=', '>', '<', '?', ':', '=', ':=', '+=', '-=', '*=', '/=', ',']
const TRAILING_WORDS = ['and', 'or', 'not']
const BLOCK_START = /^(?:(?:var\s+|varip\s+)?(?:[A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_]\w*)*|\[[^\]]*\])\s*:?=\s*)?(if|for|while|switch)\b|^else\b/

const indentOf = (line) => line.match(/^[ \t]*/)[0].replace(/\t/g, '    ').length

/** One line with comments removed and its strings blanked; also an unterminated string's column. */
function scanLine(line) {
  let out = ''
  let quote = null
  let qcol = 0
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (quote) {
      if (ch === '\\') {
        out += '  '
        i++
        continue
      }
      out += ch === quote ? ch : ' '
      if (ch === quote) quote = null
      continue
    }
    if (ch === '/' && line[i + 1] === '/') break
    if (ch === '"' || ch === "'") {
      quote = ch
      qcol = i
    }
    out += ch
  }
  return { code: out.replace(/\s+$/, ''), openString: quote ? qcol : null }
}

// The top-level argument count of the call whose `(` is at `open` in `code`.
function argCount(code, open) {
  let depth = 0
  let n = 0
  let any = false
  for (let i = open + 1; i < code.length; i++) {
    const ch = code[i]
    if (ch === '(' || ch === '[') depth++
    else if (ch === ')' || ch === ']') {
      if (depth === 0) return any ? n + 1 : 0
      depth--
    } else if (ch === ',' && depth === 0) n++
    else if (!/\s/.test(ch)) any = true
  }
  return null // spans lines; not judged
}

export function lintSource(source) {
  const issues = []
  const add = (line, col, message) => issues.push({ line, col, message })
  const lines = String(source ?? '').split('\n')
  const scanned = lines.map(scanLine)
  const stack = []
  let stmtIndent = 0
  let continuing = false
  const nextCode = (i) => {
    for (let j = i + 1; j < lines.length; j++) if (scanned[j].code.trim()) return j
    return -1
  }
  for (let i = 0; i < lines.length; i++) {
    const { code, openString } = scanned[i]
    const ln = i + 1
    if (openString != null) add(ln, openString + 1, 'this string is not closed on its line')
    if (!code.trim()) continue
    if (!continuing && stack.length === 0) stmtIndent = indentOf(lines[i])
    for (let c = 0; c < code.length; c++) {
      const ch = code[c]
      if (ch === '(' || ch === '[') stack.push({ ch, line: ln, col: c + 1 })
      else if (ch === ')' || ch === ']') {
        const want = ch === ')' ? '(' : '['
        const top = stack.pop()
        if (!top) add(ln, c + 1, `\`${ch}\` closes nothing`)
        else if (top.ch !== want) add(ln, c + 1, `\`${ch}\` closes the \`${top.ch}\` opened on line ${top.line}`)
      }
    }
    for (const m of code.matchAll(/\b((?:ta|math)\.[a-z_]+|nz|na|fixnan)\s*\(/g)) {
      const range = ARITY[m[1]]
      if (!range) continue
      const n = argCount(code, m.index + m[0].length - 1)
      if (n == null) continue
      const [lo, hi] = range
      if (n < lo || n > hi) {
        const want = lo === hi ? `${lo}` : hi >= 99 ? `at least ${lo}` : `${lo} to ${hi}`
        add(ln, m.index + 1, `${m[1]} takes ${want} argument${lo === 1 && hi === 1 ? '' : 's'}, not ${n}`)
      }
    }
    const t = code.trim()
    const word = t.match(/\b(\w+)$/)?.[1]
    const op = TRAILING_OPS.find((o) => t.endsWith(o) && !t.endsWith('=>') && !(o === '>' && t.endsWith('=>')))
    const trailing = (op && !(op === '-' && t.endsWith('--'))) || TRAILING_WORDS.includes(word)
    const j = nextCode(i)
    if (trailing || stack.length > 0) {
      if (j < 0 || indentOf(lines[j]) <= stmtIndent) {
        if (stack.length > 0 && !trailing) {
          // reported once at the end if never closed
        } else add(ln, code.length, `line ${ln} ends with \`${op ?? word}\` but the next line does not continue it (indent the continuation)`)
      }
      continuing = j >= 0 && indentOf(lines[j]) > stmtIndent
    } else continuing = false
    const opensBlock = BLOCK_START.test(t) || t.endsWith('=>')
    if (opensBlock && !continuing && stack.length === 0 && !/=>\s*\S/.test(t)) {
      if (j < 0 || indentOf(lines[j]) <= indentOf(lines[i])) {
        add(ln, indentOf(lines[i]) + 1, `the block after line ${ln} needs its lines indented under it`)
      }
    }
  }
  for (const o of stack) add(o.line, o.col, `\`${o.ch}\` is never closed`)
  return issues.sort((a, b) => a.line - b.line || a.col - b.col)
}

/** Where a PineTS error happened, when the message says or names something the source has. */
export function locateError(source, message) {
  const m = String(message ?? '')
  const at = m.match(/\bat (\d+):(\d+)\b/)
  if (at) return { line: Number(at[1]), col: Number(at[2]) }
  const name = m.match(/^([\w.$]+) is not (?:defined|a function)/)?.[1]
  if (!name) return null
  const lines = String(source ?? '').split('\n')
  const re = new RegExp(`(^|[^\\w.])${name.replace(/[.$]/g, (c) => `\\${c}`)}(?![\\w])`)
  for (let i = 0; i < lines.length; i++) {
    const { code } = scanLine(lines[i])
    const hit = code.match(re)
    if (hit) return { line: i + 1, col: hit.index + hit[1].length + 1 }
  }
  return null
}
