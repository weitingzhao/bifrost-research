// bifrost pine-runner — runs Pine Script over OHLCV bars and returns the sessions
// each script's `buy` / `sell` plots fired; on request also numeric plot series
// and a strategy()'s trades (0.2.0), and serves named context series (IV, VRP,
// earnings, SPY) to request.security (0.3.0). Since 0.4.0 the scripts run in a
// worker thread with a deadline per series, so a heavy script cannot hold the
// runner. Research calls it over HTTP; nothing here touches a database or a
// broker (D10). Licensed AGPL-3.0-only: it links PineTS
// (https://github.com/LuxAlgo/PineTS), and stays a separate process so that
// licence does not reach bifrost-research or the frontend.
import http from 'node:http'
import { readFileSync } from 'node:fs'
import { Worker } from 'node:worker_threads'
import { validateContext, validateOptions, validateSource } from './pine.mjs'

export { runScript, securityCalls, validateContext, validateOptions, validateSource } from './pine.mjs'

// The runner pins the engine; reported on /health and every /run response.
const PKG = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))
const PINETS_VERSION = PKG.dependencies.pinets
const RUNNER_VERSION = PKG.version
const PORT = Number(process.env.PORT || 8797)
const MAX_BODY = 64 * 1024 * 1024
// Pine sources are transpiled to JS and run in this process's worker, so a
// script can reach anything the process can. The image starts node with the
// permission model (Dockerfile CMD): reads limited to the app, no child
// processes, addons or WASI, workers only for this runner's own; the
// NetworkPolicy in k8s/pine/ denies all egress.
const SANDBOXED = Boolean(process.permission)
// One series takes tens of milliseconds (a built-in over six years ~0.1 s); a
// script still running after this is stopped and that series reports it.
export const SERIES_TIMEOUT_MS = Number(process.env.SERIES_TIMEOUT_MS || 10_000)
// Heap for the scripts; the container's limit is 1 Gi. This bounds a script that
// grows slowly; one allocation far past it is not caught — V8 aborts the whole
// process (Node 25, measured 2026-10-06) and Kubernetes restarts the pod.
const WORKER_HEAP_MB = Number(process.env.WORKER_HEAP_MB || 512)

export class ScriptTimeout extends Error {}

/**
 * One worker thread, one series at a time, in arrival order across requests.
 * A series past its deadline (or out of memory) terminates the worker; the next
 * series starts a fresh one.
 */
export class ScriptPool {
  constructor({ timeoutMs = SERIES_TIMEOUT_MS, heapMb = WORKER_HEAP_MB } = {}) {
    this.timeoutMs = timeoutMs
    this.heapMb = heapMb
    this.worker = null
    this.queue = []
    this.busy = false
    this.seq = 0
    this.restarts = 0
  }

  run(source, bars, opts) {
    return new Promise((resolve, reject) => {
      this.queue.push({ source, bars, opts, resolve, reject })
      this.#next()
    })
  }

  #spawn() {
    // A test seam: tests stand in a factory to play a refused Worker.
    const make = globalThis.__pineWorkerFactory ?? ((url, o) => new Worker(url, o))
    this.worker = make(new URL('./worker.mjs', import.meta.url), {
      resourceLimits: { maxOldGenerationSizeMb: this.heapMb },
    })
    this.worker.unref()
  }

  #kill() {
    const w = this.worker
    this.worker = null
    this.restarts += 1
    if (w) void w.terminate()
  }

  #next() {
    if (this.busy || this.queue.length === 0) return
    this.busy = true
    const task = this.queue.shift()
    if (!this.worker) {
      try {
        this.#spawn()
      } catch (e) {
        // e.g. the permission model without --allow-worker: say so, keep the queue moving
        this.busy = false
        task.reject(new Error(`the script runner could not start a worker: ${String(e?.message ?? e).slice(0, 300)}`))
        this.#next()
        return
      }
    }
    const w = this.worker
    const id = ++this.seq
    let settled = false
    const done = (fn, arg) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      w.off('message', onMessage)
      w.off('error', onError)
      w.off('exit', onExit)
      this.busy = false
      fn(arg)
      this.#next()
    }
    const onMessage = (m) => {
      if (m?.id !== id) return
      if (m.ok) done(task.resolve, m.result)
      else done(task.reject, new Error(m.error))
    }
    const onError = (e) => {
      this.#kill()
      const oom = e?.code === 'ERR_WORKER_OUT_OF_MEMORY'
      done(task.reject, new Error(oom ? `the script ran out of memory (${this.heapMb} MB)` : String(e?.message ?? e).slice(0, 500)))
    }
    const onExit = () => {
      if (this.worker === w) this.worker = null
      done(task.reject, new Error('the script runner stopped; try again'))
    }
    const timer = setTimeout(() => {
      this.#kill()
      done(task.reject, new ScriptTimeout(`the script ran longer than ${this.timeoutMs / 1000} s and was stopped`))
    }, this.timeoutMs)
    w.on('message', onMessage)
    w.on('error', onError)
    w.on('exit', onExit)
    w.postMessage({ id, source: task.source, bars: task.bars, opts: task.opts })
  }
}

let pool = null
const getPool = () => (pool ??= new ScriptPool())

async function handleRun(body, scripts) {
  const err = validateSource(body?.source)
  if (err) return [400, { ok: false, error: err }]
  const series = Array.isArray(body?.series) ? body.series : null
  if (!series || series.length === 0) return [400, { ok: false, error: 'series is required' }]
  const optErr = validateOptions(body) ?? validateContext(body)
  if (optErr) return [400, { ok: false, error: optErr }]
  const opts = { plots: body.plots, trades: body.trades === true, market: body.market }
  const results = []
  // A script that overran on one symbol is as heavy on the next: the rest of
  // the request is not run, so a request costs at most one deadline.
  let overran = null
  for (const s of series) {
    if (overran) {
      results.push({ symbol: s.symbol, error: `not run: the script ran out of time on ${overran}` })
      continue
    }
    const bars = Array.isArray(s?.bars) ? s.bars : []
    try {
      const r = await scripts.run(body.source, bars, { ...opts, symbol: s.symbol, context: s.context })
      const row = { symbol: s.symbol, buy: r.buy, sell: r.sell, plots: r.plots, warnings: r.warnings }
      if (r.series) row.series = r.series
      if ('trades' in r) row.trades = r.trades
      results.push(row)
    } catch (e) {
      if (e instanceof ScriptTimeout) overran = s.symbol
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

export function createServer({ scripts } = {}) {
  const runner = scripts ?? getPool()
  return http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      return send(res, 200, {
        ok: true,
        service: 'pine-runner',
        runner: RUNNER_VERSION,
        pinets: PINETS_VERSION,
        sandboxed: SANDBOXED,
        series_timeout_ms: runner.timeoutMs,
        worker_restarts: runner.restarts,
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
        const [status, payload] = await handleRun(body, runner)
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
