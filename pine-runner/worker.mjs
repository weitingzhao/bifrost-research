// Runs one series at a time for the HTTP server (0.4.0). The server terminates
// this thread when a script overruns its deadline, so a heavy script costs one
// series' time instead of the whole runner (a pasted script with nested loops
// held the event loop for minutes, measured 2026-10-06). Same permission model
// as the server: a worker gets no file, network or process access the server
// does not have.
import { parentPort } from 'node:worker_threads'
import { runScript } from './pine.mjs'

parentPort.on('message', async ({ id, source, bars, opts }) => {
  try {
    parentPort.postMessage({ id, ok: true, result: await runScript(source, bars, opts) })
  } catch (e) {
    parentPort.postMessage({ id, ok: false, error: String(e?.message ?? e).slice(0, 500) })
  }
})
