// bifrost pine-runner — runs Pine Script over OHLCV bars and returns the sessions
// each script's `buy` / `sell` plots fired. Research calls it over HTTP; nothing
// here touches a database or a broker (D10). Licensed AGPL-3.0-only: it links
// PineTS (https://github.com/LuxAlgo/PineTS), and stays a separate process so
// that licence does not reach bifrost-research or the frontend.
import http from 'node:http'
import { readFileSync } from 'node:fs'
import { PineTS } from 'pinets'

// The runner pins the engine; reported on /health and every /run response.
const PINETS_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))
  .dependencies.pinets
const PORT = Number(process.env.PORT || 8797)
const MAX_BODY = 64 * 1024 * 1024
const MAX_SOURCE = 200_000
const DAY_MS = 86_400_000

const SIGNAL_PLOTS = { buy: 'buy', sell: 'sell' }

function truthy(v) {
  return v === true || (typeof v === 'number' && Number.isFinite(v) && v !== 0)
}

/** bars: [{t: epoch ms of the session date, o, h, l, c, v}] oldest first. */
export async function runScript(source, bars) {
  const candles = bars.map((b) => ({
    open: b.o,
    high: b.h,
    low: b.l,
    close: b.c,
    volume: b.v ?? 0,
    openTime: b.t,
    closeTime: b.t + DAY_MS - 1,
  }))
  const { plots } = await new PineTS(candles).run(source)
  const out = { buy: [], sell: [] }
  for (const [side, title] of Object.entries(SIGNAL_PLOTS)) {
    const data = plots?.[title]?.data ?? []
    data.forEach((d, i) => {
      if (truthy(d?.value) && i < bars.length) out[side].push(bars[i].t)
    })
  }
  out.plots = Object.keys(plots ?? {}).filter((k) => !k.startsWith('__'))
  return out
}

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
  const results = []
  for (const s of series) {
    const bars = Array.isArray(s?.bars) ? s.bars : []
    try {
      const r = await runScript(body.source, bars)
      results.push({ symbol: s.symbol, buy: r.buy, sell: r.sell, plots: r.plots })
    } catch (e) {
      results.push({ symbol: s.symbol, error: String(e?.message ?? e).slice(0, 500) })
    }
  }
  return [200, { ok: true, pinets: PINETS_VERSION, results }]
}

function send(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) })
  res.end(body)
}

export function createServer() {
  return http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      return send(res, 200, { ok: true, service: 'pine-runner', pinets: PINETS_VERSION })
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
  createServer().listen(PORT, () => console.log(`pine-runner on :${PORT} (pinets ${PINETS_VERSION})`))
}
