import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { createServer, runScript, securityCalls, validateContext, validateOptions, validateSource } from './server.mjs'

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

test('refuses request.* other than same-timeframe request.security, and empty sources', () => {
  assert.equal(validateSource('x = request.security("SPY", "D", close)'), null)
  assert.match(validateSource('x = request.financial("AAPL", "EPS", "FQ")'), /only request.security/)
  assert.match(validateSource('x = request.security_lower_tf("AAPL", "5", close)'), /only request.security/)
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

// -- 0.2.0: numeric plots, strategy trades, /health under load ----------------------

const ST = `//@version=5
indicator("st", overlay=true)
[st, dir] = ta.supertrend(3.0, 10)
plot(st, "Supertrend")
plotshape(dir < 0 and dir[1] > 0, "buy")
plotshape(dir > 0 and dir[1] < 0, "sell")`

test('a numeric plot comes back as [session, value], warm-up as null', async () => {
  const r = await runScript(ST, W, { plots: ['Supertrend'] })
  const s = r.series.Supertrend
  assert.equal(s.length, W.length)
  assert.deepEqual(s.map((p) => p[0]), W.map((b) => b.t))
  assert.equal(s[0][1], null, 'no ATR on the first bar')
  assert.ok(s.slice(20).every((p) => typeof p[1] === 'number' && Number.isFinite(p[1])))
  assert.deepEqual(r.warnings, [])
  // the signals are unchanged by asking for a series
  const plain = await runScript(ST, W)
  assert.deepEqual([r.buy, r.sell], [plain.buy, plain.sell])
  assert.equal(plain.series, undefined)
  assert.equal('trades' in plain, false)
})

test('missing and repeated plot titles are reported, the first repeat is returned', async () => {
  const src = ST + '\nplot(close, "Supertrend")'
  const r = await runScript(src, W, { plots: ['Supertrend', 'nope'] })
  assert.deepEqual(r.series.nope, [])
  assert.ok(r.series.Supertrend[20][1] !== W[20].c, 'the first plot, not the repeat')
  assert.deepEqual(r.warnings.map((w) => w.code + ':' + w.title).sort(), ['duplicate_plot:Supertrend', 'missing_plot:nope'])
})

const STRAT = `//@version=5
strategy("s", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)
[st, dir] = ta.supertrend(3.0, 10)
if dir < 0 and dir[1] > 0
    strategy.entry("L", strategy.long, comment="flip up")
if dir > 0 and dir[1] < 0
    strategy.close("L", comment="flip down")
if strategy.position_size > 0
    strategy.exit("SL", "L", stop=strategy.position_avg_price * 0.97)
plotshape(dir < 0 and dir[1] > 0, "buy")`

test('strategy trades: sessions from fill time; market fills at the open, stops inside the bar', async () => {
  const r = await runScript(STRAT, W, { trades: true })
  const { closed, open } = r.trades
  assert.ok(closed.length >= 4)
  const byT = new Map(W.map((b, i) => [b.t, i]))
  const kinds = new Set()
  for (const t of closed) {
    assert.equal(t.direction, 'long')
    assert.ok(byT.has(t.entry_time) && byT.has(t.exit_time) && t.exit_time >= t.entry_time)
    // entries are market orders: filled at the open of the bar after the buy plot
    assert.equal(t.entry_at_open, true)
    assert.equal(t.entry_price, W[byT.get(t.entry_time)].o)
    assert.ok(r.buy.includes(W[byT.get(t.entry_time) - 1].t))
    assert.equal(t.entry_comment, 'flip up')
    if (t.exit_id === 'SL') {
      kinds.add('stop')
      assert.equal(t.exit_comment, 'SL', 'no comment falls back to the exit id')
      if (!t.exit_at_open) assert.ok(t.exit_price < W[byT.get(t.exit_time)].o)
    } else {
      kinds.add('close')
      assert.equal(t.exit_comment, 'flip down')
      assert.equal(t.exit_at_open, true)
      assert.equal(t.exit_price, W[byT.get(t.exit_time)].o)
    }
  }
  assert.deepEqual([...kinds].sort(), ['close', 'stop'], 'fixture: both exit kinds occur')
  assert.ok(closed.some((t) => t.exit_id === 'SL' && !t.exit_at_open), 'fixture: an intrabar stop')
  for (const t of open) assert.equal(t.exit_time, undefined)
})

test('trades on an indicator: null and a warning', async () => {
  const r = await runScript(ST, W, { trades: true })
  assert.equal(r.trades, null)
  assert.ok(r.warnings.some((w) => w.code === 'not_a_strategy'))
})

test('option validation', () => {
  assert.equal(validateOptions({}), null)
  assert.equal(validateOptions({ plots: ['a'], trades: true }), null)
  assert.match(validateOptions({ plots: 'a' }), /list/)
  assert.match(validateOptions({ plots: Array(9).fill('a') }), /at most 8/)
  assert.match(validateOptions({ trades: 'yes' }), /true or false/)
  assert.match(validateOptions({ series: Array(101).fill({}) }), /at most 100 series/)
})

test('/health answers while a large /run is computing', async () => {
  const server = createServer().listen(0)
  await new Promise((r) => server.once('listening', r))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const bars = walk(800)
    const series = Array.from({ length: 100 }, (_, i) => ({ symbol: 'S' + i, bars }))
    const t0 = performance.now()
    const run = fetch(base + '/run', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ source: ST, series, plots: ['Supertrend'] }),
    }).then((r) => r.json())
    let done = false
    run.then(() => (done = true))
    const lat = []
    await new Promise((r) => setTimeout(r, 50))
    while (!done) {
      const h0 = performance.now()
      const h = await fetch(base + '/health').then((r) => r.json())
      if (!done) lat.push(performance.now() - h0)
      assert.equal(h.ok, true)
    }
    const body = await run
    const total = performance.now() - t0
    assert.equal(body.results.length, 100)
    assert.equal(body.runner, '0.3.0')
    assert.ok(total > 500, `fixture: the run should be long enough to matter (${total.toFixed(0)} ms)`)
    assert.ok(lat.length >= 3, `/health answered ${lat.length} times during a ${total.toFixed(0)} ms run`)
    assert.ok(Math.max(...lat) < 250, `slowest /health ${Math.max(...lat).toFixed(0)} ms`)
  } finally {
    server.close()
  }
})

// -- 0.3.0: context series through request.security ------------------------------

const ctxPine = (body) => `//@version=5\nindicator("c")\n${body}`

test('request.security validation: own timeframe only, no lookahead_on', () => {
  assert.equal(validateSource(ctxPine('iv = request.security("IV_30", timeframe.period, close)')), null)
  assert.equal(validateSource(ctxPine('iv = request.security("IV_30", timeframe = timeframe.period, expression = close)')), null)
  assert.equal(validateSource(ctxPine('iv = request.security(symbol="IV_30", timeframe="1D", expression=ta.sma(close, 5))')), null)
  assert.match(validateSource(ctxPine('w = request.security(syminfo.tickerid, "W", close)')), /own timeframe only.*"W"/)
  assert.match(validateSource(ctxPine('w = request.security(syminfo.tickerid, tf, close)')), /own timeframe only.*tf/)
  assert.match(validateSource(ctxPine('w = request.security("IV_30", timeframe.period, close, lookahead=barmerge.lookahead_on)')), /lookahead_on/)
  assert.deepEqual(securityCalls('a = request.security("X", timeframe.period, f(a, b), gaps=barmerge.gaps_off)'), [
    ['"X"', 'timeframe.period', 'f(a, b)', 'gaps=barmerge.gaps_off'],
  ])
})

test('context validation: names, shapes, caps', () => {
  assert.equal(validateContext({ series: [{ context: { IV_30: [] } }], market: { SPY: [], SPY_IV_30: [] } }), null)
  assert.match(validateContext({ series: [{ context: { 'IV:30': [] } }] }), /upper-case/)
  assert.match(validateContext({ series: [{ context: { iv_30: [] } }] }), /upper-case/)
  assert.match(validateContext({ series: [{ context: { IV_30: {} } }] }), /list/)
  assert.match(validateContext({ market: [] }), /object/)
  assert.match(validateContext({ series: [{ context: { SPY: [] } }], market: { SPY: [] } }), /both/)
  const many = Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`X_${i}`, []]))
  assert.match(validateContext({ series: [{ context: many }] }), /at most 8/)
})

test('a context series is read on each session; a session it lacks is na', async () => {
  const pairs = W.map((b, i) => [b.t, i % 50 === 7 ? null : 10 + i])
  delete pairs[100] // a session the series does not carry at all
  const r = await runScript(
    ctxPine('iv = request.security("IV_30", timeframe.period, close)\nplot(iv, "iv")\nplotshape(iv > 300, "buy")'),
    W,
    { plots: ['iv'], context: { IV_30: pairs.filter(Boolean) }, symbol: 'AAPL' },
  )
  const iv = r.series.iv
  assert.deepEqual(iv.map((p) => p[0]), W.map((b) => b.t))
  for (const i of [0, 6, 8, 99, 101, 399]) assert.equal(iv[i][1], 10 + i)
  for (const i of [7, 57, 100]) assert.equal(iv[i][1], null)
  assert.deepEqual(r.buy, W.filter((_, i) => 10 + i > 300 && i % 50 !== 7).map((b) => b.t))
})

test('market bars: OHLCV under their name, na where the market has no bar', async () => {
  const spy = W.map((b) => ({ ...b, c: b.c * 2, o: b.o * 2 })).filter((_, i) => i !== 5)
  const r = await runScript(
    ctxPine('s = request.security("SPY", timeframe.period, close)\no = request.security("SPY", timeframe.period, open)\nplot(s, "s")\nplot(o, "o")\nplotshape(s > 0, "buy")'),
    W,
    { plots: ['s', 'o'], market: { SPY: spy } },
  )
  assert.equal(r.series.s[4][1], W[4].c * 2)
  assert.equal(r.series.o[4][1], W[4].o * 2)
  assert.equal(r.series.s[5][1], null)
  assert.equal(r.buy.length, W.length - 1)
})

test('an unknown series name fails that symbol with the names it carries; the runner lives on', async () => {
  await assert.rejects(
    runScript(ctxPine('x = request.security("IV_RNK", timeframe.period, close)\nplotshape(x > 1, "buy")'), W, {
      context: { IV_RANK: [] },
      market: { SPY: [] },
    }),
    /unknown series "IV_RNK"; this request carries SPY, IV_RANK/,
  )
  const server = createServer().listen(0)
  await new Promise((r) => server.once('listening', r))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const body = await fetch(base + '/run', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        source: ctxPine('x = request.security("NOPE", timeframe.period, close)\nplotshape(x > 1, "buy")'),
        series: [{ symbol: 'A', bars: W.slice(0, 50) }],
      }),
    }).then((r) => r.json())
    assert.match(body.results[0].error, /unknown series "NOPE"; this request carries none/)
    assert.equal((await fetch(base + '/health').then((r) => r.json())).ok, true)
    const bad = await fetch(base + '/run', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ source: ST, series: [{ symbol: 'A', bars: W, context: { 'X:Y': [] } }] }),
    })
    assert.equal(bad.status, 400)
  } finally {
    server.close()
  }
})

test('dates and calendar functions read the same through the provider (UTC)', async () => {
  const src = ctxPine('x = request.security(syminfo.tickerid, timeframe.period, close)\nplot(dayofweek, "dow")\nplot(dayofmonth, "dom")\nplot(x, "x")\nplotshape(x > 0, "buy")')
  const viaProvider = await runScript(src, W, { plots: ['dow', 'dom', 'x'], symbol: 'BRK.B' })
  const plain = await runScript(src.replace(/x = request[^\n]+/, 'x = close'), W, { plots: ['dow', 'dom', 'x'] })
  assert.deepEqual(viaProvider.series, plain.series)
  assert.equal(viaProvider.series.dow[0][1], new Date(W[0].t).getUTCDay() + 1)
})

// The eight built-ins, run as shipped (bars only) and through the provider
// (one request.security line added that the plots do not use): every signal,
// every plot value, every trade identical. Unused context sent alongside a
// script without request.security changes nothing either.
const LIB = new URL('../src/bifrost_research/engines/pine/library/', import.meta.url)
test('scripts without context give exactly the same results through the provider path', async () => {
  const files = readdirSync(LIB).filter((f) => f.endsWith('.pine'))
  assert.equal(files.length, 8)
  const ctx = { IV_30: walk(1200).map((b) => [b.t, b.c]) }
  for (const seed of [1, 7, 42]) {
    const bars = walk(1200, seed)
    for (const f of files) {
      const src = readFileSync(new URL(f, LIB), 'utf8')
      const plots = [...src.matchAll(/\bplot\(\s*[^,\n]+,\s*(?:title\s*=\s*)?"([^"]+)"/g)].map((m) => m[1]).slice(0, 8)
      const base = await runScript(src, bars, { plots, trades: true })
      const unused = await runScript(src, bars, { plots, trades: true, context: ctx, market: { SPY: bars } })
      const forced = await runScript(`${src}\n__ctx = request.security("IV_30", timeframe.period, close)\n`, bars, {
        plots,
        trades: true,
        context: ctx,
        market: { SPY: bars },
        symbol: 'T',
      })
      assert.ok(base.buy.length + base.sell.length > 0, `${f}: fixture fires`)
      for (const r of [unused, forced]) {
        assert.deepEqual([r.buy, r.sell], [base.buy, base.sell], `${f} seed ${seed}`)
        assert.deepEqual(r.series, base.series, `${f} seed ${seed} plots`)
        assert.deepEqual(r.trades, base.trades, `${f} seed ${seed} trades`)
        assert.deepEqual(r.warnings, base.warnings, `${f} seed ${seed} warnings`)
      }
    }
  }
})
