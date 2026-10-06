// The Pine side of the runner: what a script is sent, how it is run (PineTS),
// and what comes back. Imported by the HTTP server for validation and by the
// worker that runs the scripts (0.4.0). Licensed AGPL-3.0-only with the runner.
import { BaseProvider, PineTS } from 'pinets'

export const MAX_SOURCE = 200_000
const DAY_MS = 86_400_000

const SIGNAL_SIDES = ['buy', 'sell']
// Per-request caps on the additive options (0.2.0).
const MAX_PLOTS = 8
export const MAX_SERIES = 100
// Context series (0.3.0): names a script reads with request.security. Upper
// case with digits, `_` and `.`; never a `:` (PineTS strips an "X:" prefix).
const MAX_CONTEXT = 8
const NAME_RE = /^[A-Z][A-Z0-9_.]{0,31}$/
// request.security reads the series on the script's own timeframe only. A
// higher timeframe on the last bar of a run is the developing period (a
// Wednesday run reads that week's close so far) where history reads the last
// completed one, so a stored signal would change the next night (measured
// 2026-10-06, PineTS 0.11.0); multi-timeframe waits for that fix (ledger S5).
const SAME_TIMEFRAME = new Set(['timeframe.period', '"D"', "'D'", '"1D"', "'1D'"])

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

function candle(t, o, h, l, c, v) {
  return { open: o, high: h, low: l, close: c, volume: v, openTime: t, closeTime: t + DAY_MS - 1 }
}
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : NaN)

// A context series ([[ms, value | null], ...]) as flat candles on the script's
// own sessions: o = h = l = c = value, NaN (Pine's na) where it has none.
// Research has already aligned and forward-filled it; nothing is carried here.
function valueCandles(pairs, times) {
  const at = new Map()
  for (const p of pairs) if (Array.isArray(p)) at.set(p[0], num(p[1]))
  return times.map((t) => {
    const v = at.has(t) ? at.get(t) : NaN
    return candle(t, v, v, v, v, 0)
  })
}

// A market series given as bars ({t, o, h, l, c, v}) or as [ms, value] pairs.
function marketCandles(rows, times) {
  if (!rows.length || Array.isArray(rows[0])) return valueCandles(rows, times)
  const at = new Map(rows.map((b) => [b?.t, b]))
  return times.map((t) => {
    const b = at.get(t)
    return b ? candle(t, num(b.o ?? b.c), num(b.h ?? b.c), num(b.l ?? b.c), num(b.c), num(b.v ?? 0)) : candle(t, NaN, NaN, NaN, NaN, NaN)
  })
}

// Serves the script's own bars under its symbol and each context series under
// its name, daily only. PineTS rejects nothing on its own: an unknown name reads
// na, and an error thrown here escapes its promise and ends the process, so the
// name is recorded and the run fails after.
class SeriesProvider extends BaseProvider {
  constructor(store) {
    super({ requiresApiKey: false, providerName: 'Bifrost' })
    this.store = store
    this.unknown = new Set()
  }

  getSupportedTimeframes() {
    return new Set(['D'])
  }

  async _getMarketDataNative(tickerId, _tf, limit, sDate, eDate) {
    const rows = this.store.get(tickerId)
    if (!rows) {
      this.unknown.add(tickerId)
      return []
    }
    let out = rows
    if (sDate) out = out.filter((k) => k.openTime >= sDate)
    if (eDate) out = out.filter((k) => k.openTime <= eDate)
    if (limit) out = out.slice(-limit)
    return out.map((k) => ({ ...k }))
  }

  // UTC: the session dates are UTC midnights; another zone moves dayofweek.
  async getSymbolInfo(tickerId) {
    return {
      ticker: tickerId,
      tickerid: tickerId,
      prefix: '',
      root: tickerId,
      description: tickerId,
      type: 'stock',
      currency: 'USD',
      timezone: 'UTC',
      mintick: 0.01,
      pricescale: 100,
      minmove: 1,
      pointvalue: 1,
      session: '24x7',
    }
  }
}

/**
 * bars: [{t: epoch ms of the session date, o, h, l, c, v}] oldest first.
 * opts.plots: numeric plot titles to return as series; opts.trades: return a
 * strategy()'s closed and open trades.
 * opts.context / opts.market (0.3.0): named series for request.security — the
 * symbol's own ({name: [[ms, value | null]]}) and the request's shared ones
 * ({name: bars | [[ms, value | null]]}). A script without request.security runs
 * on the bars alone, exactly as before 0.3.0.
 */
export async function runScript(source, bars, opts = {}) {
  const candles = bars.map((b) => candle(b.t, b.o, b.h, b.l, b.c, b.v ?? 0))
  let ctx
  if (candles.length && /request\.security\s*\(/.test(source)) {
    const times = bars.map((b) => b.t)
    const symbol = typeof opts.symbol === 'string' && opts.symbol ? opts.symbol : 'MAIN'
    const store = new Map()
    for (const [name, rows] of Object.entries(opts.market ?? {})) store.set(name, marketCandles(rows, times))
    for (const [name, pairs] of Object.entries(opts.context ?? {})) store.set(name, valueCandles(pairs, times))
    store.set(symbol, candles)
    const provider = new SeriesProvider(store)
    ctx = await new PineTS(provider, symbol, 'D', null, candles[0].openTime, candles.at(-1).openTime).run(source)
    if (provider.unknown.size) {
      const sent = [...store.keys()].filter((k) => k !== symbol)
      throw new Error(
        `unknown series ${[...provider.unknown].map((n) => JSON.stringify(n)).join(', ')}; ` +
          `this request carries ${sent.length ? sent.join(', ') : 'none'}`,
      )
    }
  } else {
    ctx = await new PineTS(candles).run(source)
  }
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


// The argument list of each request.security( call, split at top-level commas.
export function securityCalls(source) {
  const out = []
  const re = /request\.security\s*\(/g
  let m
  while ((m = re.exec(source)) !== null) {
    const args = []
    let depth = 0
    let cur = ''
    let quote = null
    let i = re.lastIndex
    for (; i < source.length; i++) {
      const ch = source[i]
      if (quote) {
        cur += ch
        if (ch === quote) quote = null
        continue
      }
      if (ch === '"' || ch === "'") quote = ch
      else if (ch === '(' || ch === '[') depth++
      else if (ch === ')' || ch === ']') {
        if (depth === 0) break
        depth--
      } else if (ch === ',' && depth === 0) {
        args.push(cur.trim())
        cur = ''
        continue
      }
      cur += ch
    }
    args.push(cur.trim())
    out.push(args)
    re.lastIndex = i
  }
  return out
}

export function validateSource(source) {
  if (typeof source !== 'string' || !source.trim()) return 'source is required'
  if (source.length > MAX_SOURCE) return `source longer than ${MAX_SOURCE} characters`
  if (/request\.(?!security\s*\()[a-z_]+/.test(source))
    return 'only request.security is supported: the runner has the bars and the context series it is sent'
  if (/barmerge\.lookahead_on/.test(source)) return 'lookahead_on reads sessions that had not happened yet; leave lookahead off'
  for (const args of securityCalls(source)) {
    const named = Object.fromEntries(
      args.filter((a) => /^[a-z_]+\s*=(?!=)/.test(a)).map((a) => [a.split('=')[0].trim(), a.slice(a.indexOf('=') + 1).trim()]),
    )
    const tf = named.timeframe ?? args.filter((a) => !/^[a-z_]+\s*=(?!=)/.test(a))[1]
    if (tf === undefined || !SAME_TIMEFRAME.has(tf))
      return `request.security reads the script's own timeframe only (timeframe.period), not ${tf ?? 'none'}`
  }
  return null
}

export function validateContext(body) {
  const names = new Set()
  const check = (name, kind) => {
    if (!NAME_RE.test(name)) return `${kind} name ${JSON.stringify(name)}: upper-case letters, digits, _ and . only`
    names.add(name)
    return null
  }
  const market = body?.market
  if (market !== undefined) {
    if (market === null || typeof market !== 'object' || Array.isArray(market)) return 'market must be an object of named series'
    for (const [k, v] of Object.entries(market)) {
      const e = check(k, 'market') ?? (Array.isArray(v) ? null : `market ${k} must be a list`)
      if (e) return e
    }
  }
  for (const s of Array.isArray(body?.series) ? body.series : []) {
    const ctx = s?.context
    if (ctx === undefined) continue
    if (ctx === null || typeof ctx !== 'object' || Array.isArray(ctx)) return 'context must be an object of named series'
    for (const [k, v] of Object.entries(ctx)) {
      const e = check(k, 'context') ?? (Array.isArray(v) ? null : `context ${k} must be a list of [time, value]`)
      if (e) return e
      if (market && k in market) return `${k} is both a market and a context series`
    }
  }
  if (names.size > MAX_CONTEXT) return `at most ${MAX_CONTEXT} context series per request`
  return null
}
