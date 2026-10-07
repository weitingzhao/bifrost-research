import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { lintSource, locateError } from './lint.mjs'

const head = '//@version=5\nindicator("t")\n'
const issues = (body) => lintSource(head + body).map((i) => [i.line, i.message])

test('the slips PineTS runs without complaint are reported with their line', () => {
  assert.deepEqual(issues('a = close +\nplotshape(a > 0, "buy")'), [[3, 'line 3 ends with `+` but the next line does not continue it (indent the continuation)']])
  assert.deepEqual(issues('if close > open\nplotshape(true, "buy")'), [[3, 'the block after line 3 needs its lines indented under it']])
  assert.deepEqual(issues('a = ta.sma(close)\nplotshape(a > 0, "buy")'), [[3, 'ta.sma takes 2 arguments, not 1']])
  assert.deepEqual(issues('a = ta.highest(high, 5, 2)\nplotshape(a > 0, "buy")'), [[3, 'ta.highest takes 1 to 2 arguments, not 3']])
  assert.deepEqual(issues('a = math.max(close)\nplotshape(a > 0, "buy")'), [[3, 'math.max takes at least 2 arguments, not 1']])
  assert.deepEqual(issues('a = (close + open\nplotshape(a > 0, "buy")'), [[3, '`(` is never closed']])
  assert.deepEqual(issues('a = close + open)\nplotshape(a > 0, "buy")'), [[3, '`)` closes nothing']])
  assert.deepEqual(issues('a = ta.sma(close, 5]\nplotshape(a > 0, "buy")'), [[3, '`]` closes the `(` opened on line 3']])
  assert.deepEqual(issues('plotshape(close > 0, "buy)'), [[3, '`(` is never closed'], [3, 'this string is not closed on its line']])
  assert.deepEqual(issues('ok = close > open and\nplotshape(ok, "buy")'), [[3, 'line 3 ends with `and` but the next line does not continue it (indent the continuation)']])
})

test('continuations, blocks, functions and comments that are fine pass', () => {
  const fine = [
    'a = close +\n     open\nplotshape(a > 0, "buy")',
    'plotshape(close > open and\n     volume > 0, "buy")',
    'if close > open\n    x = 1\nplotshape(close > open, "buy")',
    'x = if close > open\n    1\nelse\n    0\nplotshape(x > 0, "buy")',
    'f(v) => v * 2\nplotshape(f(close) > 0, "buy")',
    'g(v) =>\n    w = v * 2\n    w + 1\nplotshape(g(close) > 0, "buy")',
    's = 0.0\nfor i = 0 to 10\n    s += i\nplotshape(s > 0, "buy")',
    '// a comment with ( and "quote\nplotshape(close > open, "buy") // trailing +',
    'm = switch\n    close > open => 1\n    => 0\nplotshape(m > 0, "buy")',
    '[a, b] = ta.supertrend(3, 10)\nplotshape(b < 0, "buy")',
    't = "a \\"quoted\\" word"\nplotshape(close > 0, "buy")',
  ]
  for (const body of fine) assert.deepEqual(lintSource(head + body), [], body)
})

test('the eight built-ins and every pre-registered script pass', () => {
  const lib = new URL('../src/bifrost_research/engines/pine/library/', import.meta.url)
  const files = readdirSync(lib).filter((f) => f.endsWith('.pine'))
  assert.equal(files.length, 8)
  for (const f of files) assert.deepEqual(lintSource(readFileSync(new URL(f, lib), 'utf8')), [], f)
  let n = 0
  for (const p of ['PREREG-pine-options-context-2026-10-06.md', 'PREREG-pine-name-selection-2026-10-06.md', 'PREREG-pine-etf-timing-2026-10-06.md']) {
    let md
    try {
      md = readFileSync(`/Users/vision-mac-trader/Desktop/stocks/${p}`, 'utf8')
    } catch {
      continue // not on this machine (CI)
    }
    for (const m of md.matchAll(/```pine\n([\s\S]*?)```/g)) {
      assert.deepEqual(lintSource(m[1]), [], `${p}: ${m[1].slice(0, 60)}`)
      n++
    }
  }
  assert.ok(n === 0 || n === 10, `pre-registered scripts checked: ${n}`)
})

test('PineTS errors are located where the message allows', () => {
  const src = head + 'a = ta.nosuch(close, 5)\nb = foo + 1\nplotshape(a > 0, "buy")'
  assert.deepEqual(locateError(src, 'ta.nosuch is not a function'), { line: 3, col: 5 })
  assert.deepEqual(locateError(src, 'foo is not defined'), { line: 4, col: 5 })
  assert.deepEqual(locateError(src, 'Failed to transpile Pine Script version 5: Unterminated string at 2:40'), { line: 2, col: 40 })
  assert.equal(locateError(src, 'something else'), null)
})
