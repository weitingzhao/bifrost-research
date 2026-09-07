"""Cheap triage between proposing candidates and judging them.

The loop proposed eight candidates and sent all eight to the judges. A judge is
an agent with tools, so it costs about $0.077 a candidate across the two models,
and the eight-candidate run measured on 2026-09-07 spent $0.63 of which $0.62
was judging. Nothing in between asked the cheaper question first: of these
eight, which are worth that?

This stage asks it in one chat call with no tools, over the compact evidence the
candidates already carry. Measured on DEV against a real batch it cost $0.00013
for three candidates — 558 input tokens, 81 output, 2.1 seconds. Judging three
names instead of eight saves about $0.39, which is three thousand times what the
ranking cost. The asymmetry is the whole design: the expensive stage is expensive
because each judge is an agent that calls tools in a loop, and this one is not.

It is advisory by default. ``policy.triage.deep_judge_top_n`` is 0 unless the
Owner sets it, so a run behaves exactly as before and the ranking is only
recorded. Turning it on is a deliberate act with a visible number attached.

D10: advisory only, like every other stage here. Nothing this module produces
reaches an order.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from bifrost_research.copilot.models import ModelConfigError, resolve_chat_endpoint
from bifrost_research.copilot.providers import estimate_cost

logger = logging.getLogger(__name__)

DEFAULT_TRIAGE_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS = 45.0
#: Cap on candidates described to the model, so a runaway propose stage cannot
#: turn the cheap stage into an expensive one.
MAX_ITEMS = 40

TRACE_KEYS = (
    "status",
    "source",
    "model",
    "provider",
    "deep_judge_top_n",
    "ranked",
    "deep",
    "held",
    "picked_by_owner",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "elapsed_ms",
    "error",
)


def _mapping(value: Any) -> dict[str, Any]:
    """Named for uniqueness, not style: `_dict` already exists in six modules
    and the code-health ratchet counts duplicated function names."""
    return value if isinstance(value, dict) else {}


def triage_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    return _mapping(_mapping(policy).get("triage"))


def triage_enabled(policy: dict[str, Any] | None) -> bool:
    raw = triage_policy(policy).get("enabled")
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes")
    return bool(raw)


def triage_model(policy: dict[str, Any] | None) -> str:
    raw = triage_policy(policy).get("model")
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else DEFAULT_TRIAGE_MODEL


def deep_judge_top_n(policy: dict[str, Any] | None) -> int:
    """How many candidates go on to the judges. 0 means all of them."""
    raw = triage_policy(policy).get("deep_judge_top_n")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    """The few facts worth paying a model to read.

    The full evidence block is what the judges get. Handing the same thing to
    triage would make the cheap stage cost what the expensive one does, which
    would defeat it.
    """
    evidence = _mapping(item.get("evidence"))
    selection = _mapping(evidence.get("selection"))
    price = _mapping(evidence.get("price_context"))
    options = _mapping(evidence.get("option_analytics"))
    track = _mapping(evidence.get("track_record"))
    horizons = track.get("horizons") if isinstance(track.get("horizons"), list) else []
    best = horizons[-1] if horizons else {}
    out: dict[str, Any] = {"symbol": str(item.get("symbol") or "").upper()}
    if item.get("score") is not None:
        out["score"] = item.get("score")
    if selection:
        out["selection"] = {
            k: selection.get(k)
            for k in ("path", "grade", "stage", "sepa_score", "status")
            if selection.get(k) is not None
        }
    if price:
        out["price"] = {
            k: price.get(k)
            for k in ("close", "sma_50", "sma_200", "pct_off_52w_high")
            if price.get(k) is not None
        }
    if options:
        out["options"] = {
            k: options.get(k)
            for k in ("iv_rank_1y", "terrain_regime", "total_net_gex")
            if options.get(k) is not None
        }
    if isinstance(best, dict) and best:
        out["settled_record"] = {
            k: best.get(k) for k in ("horizon_days", "judged", "hit_rate") if best.get(k) is not None
        }
    return out


def triage_messages(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    compact = [compact_item(i) for i in items[:MAX_ITEMS]]
    system = (
        "You triage research candidates for an Owner. This is advisory only; "
        "nothing you write places a trade.\n"
        "A deep review by two judge models costs about $0.077 per candidate, so "
        "your job is to say which candidates are worth that and which are not.\n"
        "Reply with ONLY one JSON object:\n"
        '{"ranked":[{"symbol":"AAA","worth":0.0,"why":"one short sentence",'
        '"why_zh":"同一句话的简体中文"}]}\n'
        "Include every symbol you were given, exactly once, ordered most worth "
        "reviewing first. `worth` is 0.0 to 1.0. In `why`, name the specific "
        "evidence that decided it — a grade, a level, a settled hit rate — not a "
        "general statement about the market. Say plainly when a candidate is thin "
        "on evidence; that is a reason to rank it low, not a reason to hedge. "
        "`why_zh` is the same sentence in Simplified Chinese, keeping tickers, "
        "numbers and terms of art as they are."
    )
    user = "Candidates:\n" + json.dumps(compact, default=str)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_ranked(raw: str, symbols: set[str]) -> list[dict[str, Any]] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError:
        return None
    rows = payload.get("ranked")
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        # A symbol the run never proposed cannot be ranked into it. Without this
        # a hallucinated ticker would reach the judges as if the loop had picked
        # it, and the audit trail would say the loop did.
        if sym not in symbols or sym in seen:
            continue
        seen.add(sym)
        try:
            worth = float(r.get("worth"))
        except (TypeError, ValueError):
            worth = 0.0
        row = {
            "symbol": sym,
            "worth": max(0.0, min(1.0, worth)),
            "why": str(r.get("why") or "").strip()[:300],
        }
        why_zh = str(r.get("why_zh") or "").strip()
        if why_zh:
            row["why_zh"] = why_zh[:300]
        out.append(row)
    if not out:
        return None
    # Anything the model dropped keeps its place at the back rather than
    # vanishing: a candidate the loop proposed must still be accounted for.
    for sym in sorted(symbols - seen):
        out.append({"symbol": sym, "worth": 0.0, "why": "not ranked by the triage model"})
    return out


def heuristic_ranked(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by the score the selection stage already computed."""

    def score_of(i: dict[str, Any]) -> float:
        try:
            return float(i.get("score"))
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(items, key=score_of, reverse=True)
    top = score_of(ordered[0]) if ordered else 0.0
    return [
        {
            "symbol": str(i.get("symbol") or "").upper(),
            "worth": round(score_of(i) / top, 4) if top > 0 else 0.0,
            "why": f"selection score {score_of(i):.4g} (no model ranking)",
        }
        for i in ordered
    ]


def split_for_deep(
    items: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    top_n: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split the proposed items into the ones to judge and the ones to hold.

    A held candidate keeps its place in the batch with no verdicts, which the
    leash already treats as not-accepted. Holding is never silently an approval.
    """
    if top_n <= 0 or top_n >= len(items):
        return list(items), []
    order = {r["symbol"]: n for n, r in enumerate(ranked)}
    by_symbol = {str(i.get("symbol") or "").upper(): i for i in items}
    chosen = [r["symbol"] for r in ranked[:top_n] if r["symbol"] in by_symbol]
    deep = [by_symbol[s] for s in chosen]
    held = [s for s in sorted(by_symbol, key=lambda s: order.get(s, 10**6)) if s not in set(chosen)]
    return deep, held


def run_triage(
    items: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Rank the proposed candidates by whether a deep review is worth its cost."""
    import httpx

    symbols = {str(i.get("symbol") or "").upper() for i in items if i.get("symbol")}
    model = triage_model(policy)
    out: dict[str, Any] = {
        "status": "ok",
        "source": "heuristic",
        "model": model,
        "provider": None,
        "deep_judge_top_n": deep_judge_top_n(policy),
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "elapsed_ms": 0,
        "error": None,
        "ranked": heuristic_ranked(items),
    }
    if not items:
        out["status"] = "skipped"
        return out

    try:
        endpoint = resolve_chat_endpoint(model)
    except ModelConfigError as exc:
        out["error"] = str(exc)[:200]
        return out
    out["provider"] = endpoint.provider

    api_key = endpoint.api_key
    if not api_key:
        out["error"] = f"{endpoint.api_key_env} not configured"
        return out

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                f"{endpoint.base_url}/chat/completions",
                json={
                    "model": endpoint.model,
                    "messages": triage_messages(items),
                    "temperature": 0.1,
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        out["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        out["error"] = f"httpx error {exc}"[:200]
        return out

    out["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    out["calls"] = 1
    if resp.status_code >= 400:
        out["error"] = f"HTTP {resp.status_code} {resp.text[:120]}"
        return out
    try:
        payload = resp.json()
    except ValueError:
        out["error"] = "non-json response"
        return out

    usage = payload.get("usage") or {}
    out["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    out["output_tokens"] = int(usage.get("completion_tokens") or 0)
    out["cost_usd"] = round(
        estimate_cost(endpoint.model, out["input_tokens"], out["output_tokens"]), 6
    )

    choices = payload.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        text = str((choices[0].get("message") or {}).get("content") or "")
    ranked = _parse_ranked(text, symbols)
    if ranked is None:
        # The spend already happened, so it stays on the record; only the
        # ranking falls back. Reporting cost 0 here would understate the bill.
        out["error"] = "unparseable triage response"
        return out
    out["ranked"] = ranked
    out["source"] = "llm"
    return out


def triage_decision(result: dict[str, Any], deep: list[dict[str, Any]], held: list[str]) -> str:
    """The one line the stepper shows for this stage."""
    n = len(result.get("ranked") or [])
    if result.get("status") == "skipped":
        return "no candidates to triage"
    base = f"source={result.get('source')} ranked={n}"
    if held:
        base += f" deep={len(deep)} held={len(held)}"
    else:
        base += f" deep={len(deep)} (advisory)"
    if result.get("cost_usd"):
        base += f" ${result['cost_usd']:.4f}"
    if result.get("error"):
        base += f" error={result['error'][:60]}"
    return base


def triage_stage(
    items: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None,
    trace: list[dict[str, Any]],
    flush: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Run the stage and record it. Returns ``(judge_items, summary, held)``.

    The stage owns its own tracing so the runtime carries one call rather than a
    block, and so the trace keys stay next to the code that produces them.

    Every failure path returns every candidate. A triage that cannot rank must
    not narrow: the run falls back to judging everything, which is what it did
    before this stage existed — the expensive outcome, never the silently narrow
    one.
    """
    if not items or not triage_enabled(policy):
        return list(items), None, []
    # The Owner may name the candidates outright, which skips the ranking's
    # authority over the split without skipping the ranking itself: the batch
    # still records what the model thought of every name.
    picked = {
        str(s).strip().upper()
        for s in (triage_policy(policy).get("symbols") or [])
        if str(s).strip()
    }
    try:
        summary = run_triage(items, policy=policy)
        if picked:
            judge_items = [i for i in items if str(i.get("symbol") or "").upper() in picked]
            held = [
                str(i.get("symbol") or "").upper()
                for i in items
                if str(i.get("symbol") or "").upper() not in picked
            ]
            summary["picked_by_owner"] = sorted(picked)
        else:
            judge_items, held = split_for_deep(
                items, summary.get("ranked") or [], deep_judge_top_n(policy)
            )
        summary["deep"] = [str(i.get("symbol") or "") for i in judge_items]
        summary["held"] = held
        detail = triage_decision(summary, judge_items, held)
        trace.append(
            {
                "step": "triage",
                "label": "Triage",
                "decision": detail,
                **{k: summary[k] for k in TRACE_KEYS if k in summary},
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("triage failed: %s", exc)
        judge_items, held = list(items), []
        summary = {"status": "error", "error": str(exc)[:200]}
        detail = f"error {str(exc)[:80]}"
        trace.append({"step": "triage", "label": "Triage", "decision": "error", **summary})
    if callable(flush):
        flush(step="triage", label="Triage", detail=detail[:120])
    return judge_items, summary, held


__all__ = [
    "DEFAULT_TRIAGE_MODEL",
    "TRACE_KEYS",
    "compact_item",
    "deep_judge_top_n",
    "heuristic_ranked",
    "run_triage",
    "split_for_deep",
    "triage_decision",
    "triage_enabled",
    "triage_messages",
    "triage_model",
    "triage_stage",
]
