// bifrost pine-runner — runs Pine Script over OHLCV bars and returns the sessions
// each script's `buy` / `sell` plots fired; on request also numeric plot series
// and a strategy()'s trades (0.2.0). Research calls it over HTTP; nothing
// here touches a database or a broker (D10). Licensed AGPL-3.0-only: it links
// PineTS (https://github.com/LuxAlgo/PineTS), and stays a separate process so
// that licence does not reach bifrost-research or the frontend.
import http from 'node:http'
import { readFileSync } from 'node:fs'
import { PineTS } from 'pinets'

// The runner pins the engine; reported on /health and every /run response.
const PKG = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))
const PINETS_VERSION = PKG.dependencies.pinets
const RUNNER_VERSION = PKG.version
const PORT = Number(process.env.PORT || 8797)
const MAX_BODY = 64 * 1024 * 1024
const MAX_SOURCE = 200_000
const DAY_MS = 86_400_000
// Pine sources are transpiled to JS and run in this process, so a script can
// reach anything the process can. The image starts node with the permission
// model (Dockerfile CMD): reads limited to the app, no child processes, workers,
// addons or WASI; the NetworkPolicy in k8s/pine/ denies all egress.
const SANDBOXED = Boolean(process.permission)

const SIGNAL_SIDES = ['buy', 'sell']
// Per-request caps on the additive options (0.2.0).
const MAX_PLOTS = 8
const MAX_SERIES = 100

function truthy(v) {
  return v === true || (typeof v === 'number' && Number.isFinite(v) && v !== 0)
}

// PineTS keys plots by title, and repeats change the key: two plotshape() calls
// titled "buy" merge into one interleaved series ("buy", two points per bar),
// and a repeated plot() title is renamed ("buy#3"). Every point carries its
// bar's openTime, so the session comes from d.time, never from the index.
function titleOf(key, plot) {
  const t = typeof plot?.title === 'string' ? plot.title : plot?.data?.find((d) => typeof d?.title === 'string')?.title
  return t ?? key.replace(/#\d+$/, '')
}

function finite(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

// A numeric plot by title, as [[session ms, value | null], ...] dated by each
// point's own time. A repeated title is renamed by PineTS ("x#1"); only the
// first plot with the title is returned and the repeat is reported.
function plotSeries(plots, keys, title, sessions, warnings) {
  const matches = keys.filter((k) => titleOf(k, plots[k]) === title)
  if (matches.length === 0) {
    warnings.push({ code: 'missing_plot', title, message: `no plot is titled "${title}"` })
    return []
  }
  if (matches.length > 1) {
    warnings.push({
      code: 'duplicate_plot',
      title,
      count: matches.length,
      message: `${matches.length} plots are titled "${title}"; the first is returned`,
    })
  }
  const out = []
  const seen = new Set()
  const data = Array.isArray(plots[matches[0]]?.data) ? plots[matches[0]].data : []
  for (const d of data) {
    if (!sessions.has(d?.time) || seen.has(d.time)) continue
    seen.add(d.time)
    out.push([d.time, finite(d.value)])
  }
  return out
}

// A fill at the bar's open was decided by the previous close (a market order,
// or a stop/limit the open gapped through); anything else filled inside the
// bar, and was known only once the bar was trading.
function atOpen(price, bar, slippageTicks) {
  if (!bar || typeof price !== 'number') return false
  const tol = 1e-9 * Math.abs(bar.open) + (slippageTicks > 0 ? slippageTicks * 0.01 + 1e-9 : 0)
  return Math.abs(price - bar.open) <= tol
}

// strategy() trades, both sides of each dated by the session the fill fell on.
function strategyTrades(strategy, candles) {
  const slip = Number(strategy?.config?.slippage) || 0
  const row = (t, closed) => {
    const out = {
      direction: t.size > 0 ? 'long' : 'short',
      qty: Math.abs(t.size),
      entry_time: t.entry_time,
      entry_price: finite(t.entry_price),
      entry_id: t.entry_id ?? null,
      entry_comment: t.entry_comment ?? t.entry_id ?? null,
      entry_at_open: atOpen(t.entry_price, candles[t.entry_bar_index], slip),
    }
    if (!closed) return out
    return {
      ...out,
      exit_time: t.exit_time,
      exit_price: finite(t.exit_price),
      exit_id: t.exit_id ?? null,
      exit_comment: t.exit_comment ?? t.exit_id ?? null,
      exit_at_open: atOpen(t.exit_price, candles[t.exit_bar_index], slip),
      profit: finite(t.profit),
    }
  }
  return {
    closed: (strategy.closedtrades ?? []).map((t) => row(t, true)),
    open: (strategy.opentrades ?? []).map((t) => row(t, false)),
  }
}

/**
 * bars: [{t: epoch ms of the session date, o, h, l, c, v}] oldest first.
 * opts.plots: numeric plot titles to return as series; opts.trades: return a
 * strategy()'s closed and open trades.
 */
export async function runScript(source, bars, opts = {}) {
  const candles = bars.map((b) => ({
    open: b.o,
    high: b.h,
    low: b.l,
    close: b.c,
    volume: b.v ?? 0,
    openTime: b.t,
    closeTime: b.t + DAY_MS - 1,
  }))
  const ctx = await new PineTS(candles).run(source)
  const { plots } = ctx
  const sessions = new Set(bars.map((b) => b.t))
  const keys = Object.keys(plots ?? {}).filter((k) => !k.startsWith('__'))
  const out = { buy: [], sell: [], plots: keys, warnings: [] }
  for (const side of SIGNAL_SIDES) {
    const fired = new Set()
    let calls = 0
    for (const key of keys) {
      const plot = plots[key]
      if (titleOf(key, plot) !== side) continue
      const data = Array.isArray(plot?.data) ? plot.data : []
      // a merged key holds one point per bar per call
      calls += bars.length ? Math.max(1, Math.round(data.length / bars.length)) : 1
      for (const d of data) {
        if (truthy(d?.value) && sessions.has(d?.time)) fired.add(d.time)
      }
    }
    out[side] = [...fired].sort((a, b) => a - b)
    if (calls > 1) {
      out.warnings.push({
        code: 'duplicate_title',
        side,
        count: calls,
        message: `${calls} plots are titled "${side}"; their sessions were merged (fired on any)`,
      })
    }
    if (calls === 0) {
      out.warnings.push({ code: 'missing_title', side, message: `no plot is titled "${side}"; no ${side} signals` })
    }
  }
  const titles = Array.isArray(opts.plots) ? opts.plots : []
  if (titles.length) {
    out.series = {}
    for (const title of titles) out.series[title] = plotSeries(plots ?? {}, keys, title, sessions, out.warnings)
  }
  if (opts.trades) {
    if (ctx.strategy) out.trades = strategyTrades(ctx.strategy, candles)
    else {
      out.trades = null
      out.warnings.push({ code: 'not_a_strategy', message: 'the script is an indicator(); it has no trades' })
    }
  }
  return out
}

export function validateOptions(body) {
  const plots = body?.plots
  if (plots !== undefined) {
    if (!Array.isArray(plots) || plots.some((p) => typeof p !== 'string' || !p.trim()))
      return 'plots must be a list of plot titles'
    if (plots.length > MAX_PLOTS) return `at most ${MAX_PLOTS} plots`
  }
  if (body?.trades !== undefined && typeof body.trades !== 'boolean') return 'trades must be true or false'
  if (Array.isArray(body?.series) && body.series.length > MAX_SERIES)
    return `at most ${MAX_SERIES} series per request; send the rest in another request`
  return null
}

// One series is tens of milliseconds of CPU; a request is up to MAX_SERIES of
// them. Handing the event loop back between series lets /health (the readiness
// probe) and other requests in between, so a large batch no longer starves it.
const yieldToLoop = () => new Promise((resolve) => setImmediate(resolve))

export function validateSource(source) {
  if (typeof source !== 'string' || !source.trim()) return 'source is required'
  if (source.length > MAX_SOURCE) return `source longer than ${MAX_SOURCE} characters`
  if (/request\.security|request\.seed|request\.financial|request\.economic/.test(source))
    return 'request.* calls are not supported: the runner only has the bars it is sent'
  return null
}

async function handleRun(body) {
  const err = validateSource(body?.source)
  if (err) return [400, { ok: false, error: err }]
  const series = Array.isArray(body?.series) ? body.series : null
  if (!series || series.length === 0) return [400, { ok: false, error: 'series is required' }]
  const optErr = validateOptions(body)
  if (optErr) return [400, { ok: false, error: optErr }]
  const opts = { plots: body.plots, trades: body.trades === true }
  const results = []
  for (const s of series) {
    await yieldToLoop()
    const bars = Array.isArray(s?.bars) ? s.bars : []
    try {
      const r = await runScript(body.source, bars, opts)
      const row = { symbol: s.symbol, buy: r.buy, sell: r.sell, plots: r.plots, warnings: r.warnings }
      if (r.series) row.series = r.series
      if ('trades' in r) row.trades = r.trades
      results.push(row)
    } catch (e) {
      results.push({ symbol: s.symbol, error: String(e?.message ?? e).slice(0, 500) })
    }
  }
  return [200, { ok: true, pinets: PINETS_VERSION, runner: RUNNER_VERSION, results }]
}

function send(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) })
  res.end(body)
}

export function createServer() {
  return http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      return send(res, 200, {
        ok: true,
        service: 'pine-runner',
        runner: RUNNER_VERSION,
        pinets: PINETS_VERSION,
        sandboxed: SANDBOXED,
      })
    }
    if (req.method !== 'POST' || req.url !== '/run') return send(res, 404, { ok: false, error: 'not found' })
    let size = 0
    const chunks = []
    req.on('data', (c) => {
      size += c.length
      if (size > MAX_BODY) {
        send(res, 413, { ok: false, error: 'body too large' })
        req.destroy()
      } else chunks.push(c)
    })
    req.on('end', async () => {
      if (res.writableEnded) return
      let body
      try {
        body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      } catch {
        return send(res, 400, { ok: false, error: 'body must be JSON' })
      }
      try {
        const [status, payload] = await handleRun(body)
        send(res, status, payload)
      } catch (e) {
        send(res, 500, { ok: false, error: String(e?.message ?? e).slice(0, 500) })
      }
    })
  })
}

if (import.meta.url === `file://${process.argv[1]}`) {
  createServer().listen(PORT, () => console.log(`pine-runner on :${PORT} (pinets ${PINETS_VERSION}, sandboxed=${SANDBOXED})`))
}
