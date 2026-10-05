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
