import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runScript, validateSource } from './server.mjs'

const bars = []
let p = 100
for (let i = 0; i < 120; i++) {
  const o = p
  p = i < 60 ? p - 1 : p + 1.5
  bars.push({ t: Date.UTC(2024, 0, 1) + i * 86_400_000, o, h: Math.max(o, p), l: Math.min(o, p), c: p, v: 1 })
}

test('reports the sessions a buy plot fired', async () => {
  const src = `//@version=5
indicator("x")
plotshape(ta.crossover(close, ta.ema(close, 5)), "buy")
plotshape(ta.crossunder(close, ta.ema(close, 5)), "sell")`
  const r = await runScript(src, bars)
  assert.equal(r.buy.length, 1)
  assert.ok(r.buy[0] >= bars[60].t)
  assert.equal(r.sell.length, 0)
  assert.deepEqual(r.plots.sort(), ['buy', 'sell'])
})

test('refuses request.* and empty sources', () => {
  assert.match(validateSource('x = request.security("SPY", "D", close)'), /not supported/)
  assert.equal(validateSource(''), 'source is required')
  assert.equal(validateSource('//@version=5\nindicator("x")'), null)
})

// A random walk with enough crossings that two different conditions fire on
// different sessions — the case where index-based dating went wrong.
function walk(n, seed = 42) {
  let s = seed >>> 0
  const rnd = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296)
  const out = []
  let px = 100
  for (let i = 0; i < n; i++) {
    const o = px
    px = Math.max(1, o * (1 + (rnd() - 0.5) * 0.04))
    out.push({ t: Date.UTC(2023, 0, 2) + i * 86_400_000, o, h: Math.max(o, px) * 1.004, l: Math.min(o, px) * 0.996, c: px, v: 1 })
  }
  return out
}
const W = walk(400)
const A = 'ta.crossover(ta.ema(close, 9), ta.ema(close, 21))'
const B = 'ta.crossover(ta.sma(close, 5), ta.sma(close, 50))'
const pine = (body) => `//@version=5\nindicator("t")\n${body}`

test('a normal script has no warnings', async () => {
  const r = await runScript(pine(`plotshape(${A}, "buy")\nplotshape(ta.crossunder(close, ta.ema(close, 9)), "sell")`), W)
  assert.deepEqual(r.warnings, [])
  assert.ok(r.buy.length > 0 && r.sell.length > 0)
})

test('two plotshape() with one title: sessions are the union, dated by each point', async () => {
  const a = (await runScript(pine(`plotshape(${A}, "buy")`), W)).buy
  const b = (await runScript(pine(`plotshape(${B}, "buy")`), W)).buy
  assert.ok(a.length > 0 && b.length > 0)
  assert.ok(b.some((t) => !a.includes(t)), 'fixture: B fires on sessions A does not')
  const r = await runScript(pine(`plotshape(${A}, "buy")\nplotshape(${B}, "buy")`), W)
  assert.deepEqual(r.buy, [...new Set([...a, ...b])].sort((x, y) => x - y))
  assert.deepEqual(r.warnings.map((w) => [w.code, w.side, w.count ?? null]), [
    ['duplicate_title', 'buy', 2],
    ['missing_title', 'sell', null],
  ])
})

test('a repeated plot() title (renamed "buy#N") is merged too', async () => {
  const a = (await runScript(pine(`plotshape(${A}, "buy")`), W)).buy
  const b = (await runScript(pine(`plotshape(${B}, "buy")`), W)).buy
  const r = await runScript(pine(`plotshape(${A}, "buy")\nplot(${B} ? 1 : 0, "buy")`), W)
  assert.deepEqual(r.buy, [...new Set([...a, ...b])].sort((x, y) => x - y))
  assert.equal(r.warnings.find((w) => w.side === 'buy')?.code, 'duplicate_title')
})

test('untitled plots give no signals and say so', async () => {
  const r = await runScript(pine('plotshape(close > open)\nplotshape(close < open)'), W)
  assert.deepEqual([r.buy, r.sell], [[], []])
  assert.deepEqual(r.warnings.map((w) => w.code + ':' + w.side), ['missing_title:buy', 'missing_title:sell'])
})

test('every reported session is one of the bars sent', async () => {
  const r = await runScript(pine(`plotshape(${A}, "buy")\nplotshape(${B}, "buy")\nplotshape(${B}, "sell")`), W)
  const sent = new Set(W.map((b) => b.t))
  for (const t of [...r.buy, ...r.sell]) assert.ok(sent.has(t))
})
