"""One daily digest — research-loop-automation D2.

Replaces the per-hypothesis Morning Prep posts with a single ``daily_digest``
briefing per trading day: what the lenses say about holdings ∪ the candidates
the Loop proposed since yesterday, the Loop's own state (objectives, runs,
drafts waiting, trust), the hypotheses the outcome rule resolved since
yesterday, and every candidate the judges split on.

Facts are gathered by code from the readers the pages use — the exhibits, the
run digest, the repositories — and a model may write the prose over them. It
never supplies a number the facts do not carry, and when it is unavailable the
heuristic markdown stands. One digest per day: a second run the same day is a
no-op unless forced.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from bifrost_research.copilot.agents._context import gather_discoveries
from bifrost_research.copilot.harness import run_digest
from bifrost_research.repositories import ai_action_log as action_repo
from bifrost_research.repositories import ai_draft as draft_repo
from bifrost_research.repositories import candidate_pool as cand_repo
from bifrost_research.repositories import hypothesis as hyp_repo
from bifrost_research.repositories import objective as obj_repo

logger = logging.getLogger(__name__)

AGENT_ID = "digest_agent"
ACTION_SOURCE = "digest_agent"
KIND = "daily_digest"
ADVISORY = "D10 BLOCKED — research digest only; nothing here is an order."

# The lenses a symbol line carries, in reading order.
DIGEST_LENSES: tuple[str, ...] = ("iv_rank", "vrp", "gex_regime", "terrain_regime")
MAX_SYMBOLS = 20
MAX_RUNS = 50
# "Since yesterday": one trading day plus the slack between the Cron and the digest.
SINCE_HOURS = 26
DEFAULT_MODEL = "deepseek-chat"
PROSE_MAX_WORDS = 280


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _dry_run() -> bool:
    return os.environ.get("BIFROST_DIGEST_AGENT_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def digest_scope(day: date) -> str:
    return f"digest:{day.isoformat()}"


def _safe(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a missing block is a fact the digest states
        logger.debug("digest gather %s skipped: %s", getattr(fn, "__name__", fn), exc)
        return default


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _after(value: Any, since: datetime) -> bool:
    dt = _as_dt(value)
    return dt is not None and dt >= since


def existing_digest(conn: _Connection, day: date) -> dict[str, Any] | None:
    """The day's digest if one was already posted (pending or approved)."""
    for status in ("pending", "approved"):
        rows = draft_repo.list_drafts(conn, status=status, kind=KIND, scope=digest_scope(day), limit=1)
        if rows:
            return rows[0]
    return None


# ─── facts ───────────────────────────────────────────────────────────────────


def _exhibit_line(conn: _Connection, lens: str, symbol: str) -> dict[str, Any]:
    from bifrost_research.lenses.exhibits import build_exhibit

    exh = build_exhibit(conn, lens, symbol)
    verdict = exh.verdict or {}
    return {
        "lens": lens,
        "band": verdict.get("band"),
        "value": verdict.get("value"),
        "means": verdict.get("means"),
        "as_of": exh.as_of,
        "freshness": exh.freshness,
    }


def _symbol_exhibits(conn: _Connection, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for sym in symbols:
        lines: list[dict[str, Any]] = []
        for lens in DIGEST_LENSES:
            line = _safe(_exhibit_line, conn, lens, sym, default=None)
            if line is None:
                line = {"lens": lens, "band": None, "means": "reader failed", "as_of": None, "freshness": "missing"}
            lines.append(line)
        out[sym] = lines
    return out


def _holdings() -> tuple[list[str], str]:
    from bifrost_research.copilot.harness.persona_heuristic import load_held_symbols

    held, status = load_held_symbols()
    return (sorted(held) if held else []), status


def _batches_and_dissents(
    conn: _Connection, runs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Runs arrive newest first. An objective re-run with the same symbol set is
    # the same proposal, so identical batches fold into the newest one and a
    # name the judges split on is listed once per objective — the Inbox does
    # the same, and a digest that said "24 dissents" for eleven names would
    # not be read twice.
    batches: list[dict[str, Any]] = []
    dissents: list[dict[str, Any]] = []
    seen_batches: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    seen_dissent: set[tuple[str, str]] = set()
    for run in runs:
        d = _safe(run_digest.digest_run, conn, str(run.get("id")), default=None)
        if not d:
            continue
        cands = d["candidates"]
        split = [c for c in cands if c.get("net_stance") == "dissent" or c.get("blocked_by_validate")]
        objective_id = str(d["run"]["objective_id"] or "")
        key = (objective_id, tuple(c["symbol"] for c in cands))
        if key in seen_batches:
            folded = seen_batches[key]
            folded["repeats"] += 1
            folded["run_ids"].append(d["run"]["id"])
            continue
        batch = {
            "run_id": d["run"]["id"],
            "run_ids": [d["run"]["id"]],
            "repeats": 1,
            "objective_id": objective_id,
            "objective_title": d["run"]["objective_title"],
            "status": d["run"]["status"],
            "started_at": d["run"]["started_at"],
            "candidates": [c["symbol"] for c in cands],
            "draft_ids": [x["id"] for x in d["drafts"] if x.get("kind") == "candidate_batch"],
            "dissent": len(split),
            "persona_mode": (d.get("persona") or {}).get("mode"),
            "plan_generated_by": (d.get("plan") or {}).get("generated_by"),
        }
        seen_batches[key] = batch
        batches.append(batch)
        for c in split:
            if (objective_id, c["symbol"]) in seen_dissent:
                continue
            seen_dissent.add((objective_id, c["symbol"]))
            dissents.append(
                {
                    "symbol": c["symbol"],
                    "run_id": d["run"]["id"],
                    "objective_title": d["run"]["objective_title"],
                    "net_stance": c.get("net_stance"),
                    "blocked_by_validate": bool(c.get("blocked_by_validate")),
                    "judges": _judges_summary(c.get("verdicts") or []),
                    "wrong_if": ((c.get("report") or {}).get("wrong_if") or [])[:2],
                }
            )
    return batches, dissents


SEVERITY = {"oppose": 3, "caution": 2, "abstain": 1, "support": 0}


def _judges_summary(verdicts: list[dict[str, Any]]) -> list[str]:
    """One entry per judge model: its verdict agent's stance, else its most severe agent.

    The full per-agent list stays on the candidate batch; the digest names who
    split and which way, not eight chips per name.
    """
    by_model: dict[str, dict[str, str]] = {}
    for v in verdicts:
        model = str(v.get("model") or "heuristic")
        agent = str(v.get("agent") or "")
        stance = str(v.get("stance") or "abstain")
        by_model.setdefault(model, {})[agent] = stance
    out: list[str] = []
    for model, stances in by_model.items():
        stance = stances.get("verdict") or max(stances.values(), key=lambda st: SEVERITY.get(st, 0))
        out.append(f"{model}: {stance}")
    return out


def _resolutions(conn: _Connection, since: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for status in ("validated", "rejected"):
        rows = _safe(hyp_repo.list_hypotheses, conn, status=status, limit=50, include_retired=True, default=[])
        for h in rows:
            receipt = h.get("resolution_json") if isinstance(h.get("resolution_json"), dict) else {}
            when = receipt.get("resolved_at") or h.get("updated_at")
            if not _after(when, since):
                continue
            out.append(
                {
                    "id": h.get("id"),
                    "title": h.get("title"),
                    "status": status,
                    "symbols": h.get("symbols") or [],
                    "decision": receipt.get("decision"),
                    "excess": receipt.get("excess"),
                    "horizon_days": receipt.get("horizon_days"),
                    "resolved_at": when,
                    "by_rule": bool(receipt),
                }
            )
    return out


def gather_facts(conn: _Connection, *, day: date, now: datetime | None = None) -> dict[str, Any]:
    """Everything the digest says, from the readers the pages use."""
    from bifrost_research.copilot.harness.batch_orchestrate import trust_status

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=SINCE_HOURS)

    objectives = _safe(obj_repo.list_objectives, conn, status="active", limit=50, default=[])
    runs_all = _safe(obj_repo.list_runs, conn, limit=MAX_RUNS, default=[])
    runs = [r for r in runs_all if _after(r.get("started_at"), since)]
    pending = {
        kind: int(_safe(draft_repo.count_pending, conn, kind=kind, default=0) or 0)
        for kind in ("candidate_batch", "policy_suggestion", "hypothesis_suggestion", "eod_verdict")
    }
    trust = _safe(trust_status, default={"l0": False, "reason": "unavailable"})

    candidates_all = _safe(cand_repo.list_candidates, conn, status=None, days=3, limit=200, default=[])
    candidates = [c for c in candidates_all if _after(c.get("created_at"), since)]
    batches, dissents = _batches_and_dissents(conn, runs)
    resolutions = _resolutions(conn, since)
    active = _safe(hyp_repo.list_hypotheses, conn, status="active", limit=100, default=[])

    held, holdings_status = _safe(_holdings, default=([], "unavailable"))
    symbols: list[str] = []
    for sym in [*held, *(str(c.get("symbol") or "").upper() for c in candidates)]:
        if sym and sym not in symbols:
            symbols.append(sym)
    symbols = symbols[:MAX_SYMBOLS]
    exhibits = _symbol_exhibits(conn, symbols)
    discoveries = _safe(gather_discoveries, conn, limit=5, default=[])

    return {
        "day": day.isoformat(),
        "generated_at": now.isoformat(),
        "since": since.isoformat(),
        "loop": {
            "objectives": [{"id": o.get("id"), "title": o.get("title")} for o in objectives],
            "runs": [
                {"id": r.get("id"), "objective_id": r.get("objective_id"), "status": r.get("status"), "started_at": r.get("started_at")}
                for r in runs
            ],
            "pending": pending,
            "trust": trust,
        },
        "candidates": [
            {"symbol": str(c.get("symbol") or "").upper(), "source": c.get("source"), "score": c.get("score"), "status": c.get("status")}
            for c in candidates
        ],
        "batches": batches,
        "dissents": dissents,
        "resolutions": resolutions,
        "active_hypotheses": {"count": len(active), "titles": [str(h.get("title") or h.get("id")) for h in active[:10]]},
        "holdings": {"status": holdings_status, "symbols": held},
        "symbols": symbols,
        "exhibits": exhibits,
        "discoveries": discoveries,
    }


# ─── prose ───────────────────────────────────────────────────────────────────


def _band_word(line: dict[str, Any]) -> str:
    band = line.get("band")
    if not band:
        return "no reading"
    value = line.get("value")
    if isinstance(value, (int, float)):
        return f"{band} ({value:.0f})"
    if isinstance(value, str):
        return f"{band} ({value})"
    return str(band)


def _symbol_line(sym: str, lines: list[dict[str, Any]]) -> str:
    parts = [f"{ln['lens']} {_band_word(ln)}" for ln in lines]
    strongest = next((ln for ln in lines if ln.get("band") in ("hot", "cold") and ln.get("means")), None)
    tail = f" — {strongest['means']}" if strongest else ""
    return f"- **{sym}**: {' · '.join(parts)}{tail}"


def compose_markdown(facts: dict[str, Any]) -> str:
    """The digest as the heuristic writes it; the model rewrites this, never the facts."""
    loop = facts["loop"]
    pending = loop["pending"]
    trust = loop.get("trust") or {}
    runs = loop["runs"]
    awaiting = sum(1 for r in runs if r.get("status") == "awaiting_approval")
    trust_word = "L0 — auto-accept armed" if trust.get("l0") else f"not L0 ({trust.get('reason') or 'no override'})"
    out: list[str] = [f"## Daily digest · {facts['day']}", ""]
    out.append(
        f"**Loop**: {len(loop['objectives'])} active objective(s) · {len(runs)} run(s) since yesterday"
        f" ({awaiting} awaiting approval) · waiting: {pending.get('candidate_batch', 0)} candidate batch(es),"
        f" {pending.get('policy_suggestion', 0)} policy suggestion(s) · trust {trust_word}."
    )
    cands = facts["candidates"]
    res = facts["resolutions"]
    out.append(
        f"**Since yesterday**: {len(cands)} candidate(s) proposed"
        f"{' (' + ', '.join(c['symbol'] for c in cands[:12]) + ('…' if len(cands) > 12 else '') + ')' if cands else ''}"
        f" · {len(res)} hypothesis resolution(s) · {len(facts['dissents'])} dissent(s)"
        f" · {facts['active_hypotheses']['count']} active hypothesis(es)."
    )
    out.append("")
    hold = facts["holdings"]
    title = "Holdings ∪ candidates — what the lenses say" if hold.get("symbols") else "Candidates — what the lenses say"
    out.append(f"### {title}")
    if hold.get("status") != "ok" and not hold.get("symbols"):
        out.append(f"_Holdings not applied ({hold.get('status')}); symbols below are the Loop's candidates._")
    if facts["symbols"]:
        unread: list[str] = []
        for sym in facts["symbols"]:
            lines = facts["exhibits"].get(sym) or []
            if any(ln.get("band") for ln in lines):
                out.append(_symbol_line(sym, lines))
            else:
                unread.append(sym)
        if unread:
            out.append(f"- No lens readings yet (no option / terrain data): {', '.join(unread)}")
    else:
        out.append("- No holdings and no new candidates — nothing to read today.")
    out.append("")
    out.append("### Candidate batches since yesterday")
    if facts["batches"]:
        for b in facts["batches"]:
            syms = ", ".join(b["candidates"]) or "no candidates"
            repeats = f" ×{b['repeats']} runs" if b.get("repeats", 1) > 1 else ""
            out.append(
                f"- {b['objective_title'] or b['objective_id']} (`{b['run_id']}`{repeats}, {b['status']}): {syms}"
                f" — {b['dissent']} dissent(s)"
            )
    else:
        out.append("- No run since yesterday.")
    out.append("")
    out.append("### Dissents — the judges split")
    if facts["dissents"]:
        for d in facts["dissents"]:
            judges = "; ".join(d["judges"]) or "no judge record"
            wrong = f" · wrong if: {'; '.join(d['wrong_if'])}" if d["wrong_if"] else ""
            block = " · validate blocked" if d["blocked_by_validate"] else ""
            out.append(f"- **{d['symbol']}** ({d['objective_title']}): {judges}{block}{wrong}")
    else:
        out.append("- None.")
    out.append("")
    out.append("### Resolutions since yesterday")
    if res:
        for r in res:
            excess = f" excess {r['excess']:+.2%}" if isinstance(r.get("excess"), (int, float)) else ""
            rule = "by outcome rule" if r.get("by_rule") else "by hand"
            out.append(f"- {r['title']} → **{r['status']}** ({rule}{excess}; {', '.join(r['symbols'])})")
    else:
        out.append("- None — the earliest 20-session windows close later this month.")
    out.append("")
    disc = facts["discoveries"]
    if disc:
        out.append("### Discoveries")
        for d in disc[:5]:
            if d.get("source") == "sepa":
                out.append(f"- SEPA {d.get('symbol')} score {d.get('sepa_score')} grade {d.get('grade')} stage {d.get('stage')}")
            else:
                out.append(f"- Event: {d.get('title') or d.get('event_id')} ({d.get('event_type')}) {d.get('affected_symbols') or ''}")
        out.append("")
    out.append(ADVISORY)
    return "\n".join(out)


def _digest_model() -> str:
    return os.environ.get("BIFROST_DIGEST_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


PROSE_TIMEOUT_S = 60.0


def _chat_completion(model: str, messages: list[dict[str, str]], timeout_seconds: float = PROSE_TIMEOUT_S) -> tuple[str | None, dict[str, Any]]:
    """One plain chat-completions call on the same endpoint table the planner and judges use.

    Returns ``(text | None, meta)``; ``meta`` names the failure or carries the
    token counts and the estimated cost. Never raises.
    """
    import httpx

    from bifrost_research.copilot.models import resolve_chat_endpoint
    from bifrost_research.copilot.providers import estimate_cost

    meta: dict[str, Any] = {"model": model}
    try:
        endpoint = resolve_chat_endpoint(model)
    except Exception as exc:  # noqa: BLE001 — an unknown model id is a config fact, not a crash
        meta["error"] = str(exc)
        return None, meta
    meta["provider"] = endpoint.provider
    if not endpoint.api_key:
        meta["error"] = f"{endpoint.api_key_env} not configured"
        return None, meta
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                f"{endpoint.base_url}/chat/completions",
                json={"model": endpoint.model, "messages": messages, "temperature": 0.3, "stream": False},
                headers={"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        meta["error"] = f"httpx error {exc}"
        return None, meta
    if resp.status_code >= 400:
        meta["error"] = f"HTTP {resp.status_code}"
        return None, meta
    try:
        payload = resp.json()
    except ValueError:
        meta["error"] = "non-json response"
        return None, meta
    usage = payload.get("usage") or {}
    meta["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    meta["output_tokens"] = int(usage.get("completion_tokens") or 0)
    meta["cost_usd"] = round(estimate_cost(endpoint.model, meta["input_tokens"], meta["output_tokens"]), 6)
    choices = payload.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") if choices else None) or ""
    return (text.strip() or None), meta


def _optional_llm_prose(facts: dict[str, Any], fallback: str) -> tuple[str, str, dict[str, Any]]:
    """``(markdown, model, meta)`` — the model rewrites the prose over the facts, or the heuristic stands."""
    model = _digest_model()
    messages = [
        {
            "role": "system",
            "content": (
                "You write the Owner's pre-market research digest for Bifrost. "
                "Use ONLY the facts JSON and the draft below; never add a number, symbol or claim "
                "the facts do not carry. Keep the same section headers. Lead with what changed "
                "since yesterday and what needs a decision; name dissents by judge. Plain markdown, "
                f"at most {PROSE_MAX_WORDS} words. Research only — never an order. "
                f"End with exactly this line: {ADVISORY}"
            ),
        },
        {
            "role": "user",
            "content": f"FACTS JSON:\n{json.dumps(facts, default=str)[:12000]}\n\nDRAFT:\n{fallback}",
        },
    ]
    text, meta = _chat_completion(model, messages)
    if not text:
        logger.info("digest prose fell back to heuristic: %s", meta.get("error") or "empty")
        return fallback, "heuristic", meta
    if ADVISORY not in text:
        text = f"{text}\n\n{ADVISORY}"
    return text, model, meta


def build_payload(facts: dict[str, Any], *, use_llm: bool = True) -> dict[str, Any]:
    heuristic = compose_markdown(facts)
    markdown, model, meta = _optional_llm_prose(facts, heuristic) if use_llm else (heuristic, "heuristic", {})
    return {
        "title": f"Daily digest {facts['day']}",
        "day": facts["day"],
        "markdown": markdown,
        "markdown_heuristic": heuristic,
        "model": model,
        "llm_enriched": model != "heuristic",
        "prose": meta,
        "generated_at": facts["generated_at"],
        "since": facts["since"],
        "loop": facts["loop"],
        "symbols": facts["symbols"],
        "holdings_status": facts["holdings"]["status"],
        "batches": facts["batches"],
        "dissents": facts["dissents"],
        "resolutions": facts["resolutions"],
        "candidates": facts["candidates"],
        "exhibits": facts["exhibits"],
        "advisory": ADVISORY,
    }


def empty_facts(day: date) -> dict[str, Any]:
    """The facts of a day with nothing in it — what a dry run composes over."""
    now = datetime.now(timezone.utc)
    return {
        "day": day.isoformat(),
        "generated_at": now.isoformat(),
        "since": (now - timedelta(hours=SINCE_HOURS)).isoformat(),
        "loop": {"objectives": [], "runs": [], "pending": {}, "trust": {"l0": False, "reason": "dry run"}},
        "candidates": [],
        "batches": [],
        "dissents": [],
        "resolutions": [],
        "active_hypotheses": {"count": 0, "titles": []},
        "holdings": {"status": "unavailable", "symbols": []},
        "symbols": [],
        "exhibits": {},
        "discoveries": [],
    }


def run_daily_digest(
    conn: _Connection | None = None,
    *,
    day: date | None = None,
    dry_run: bool | None = None,
    force: bool = False,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Post the day's digest once. Returns ``{ok, draft_id | skipped, ...}``."""
    is_dry = _dry_run() if dry_run is None else dry_run
    day = day or datetime.now(timezone.utc).date()
    if is_dry:
        payload = build_payload(empty_facts(day), use_llm=False)
        result = {"ok": True, "dry_run": True, "kind": KIND, "scope": digest_scope(day), "payload": payload}
        print(json.dumps(result, indent=2, default=str))
        return result

    owns_conn = False
    if conn is None:
        from bifrost_research.db.conn import connect

        conn = connect()
        owns_conn = True
    try:
        existing = existing_digest(conn, day)
        if existing and not force:
            return {"ok": True, "skipped": True, "reason": "digest already posted for the day", "draft_id": existing.get("id"), "day": day.isoformat()}
        facts = gather_facts(conn, day=day)
        payload = build_payload(facts, use_llm=use_llm)
        action = action_repo.insert_action(
            conn,
            action_kind="draft_daily_digest",
            action_source=ACTION_SOURCE,
            model=payload.get("model"),
            input_payload={"day": day.isoformat(), "symbols": facts["symbols"], "runs": [r["id"] for r in facts["loop"]["runs"]]},
            output_payload={"markdown": payload["markdown"], "batches": len(facts["batches"]), "dissents": len(facts["dissents"])},
            status="proposed",
        )
        draft = draft_repo.insert_draft(
            conn,
            kind=KIND,
            payload=payload,
            scope=digest_scope(day),
            generated_by=AGENT_ID,
            linked_action_id=action["id"],
        )
        return {
            "ok": True,
            "dry_run": False,
            "draft_id": draft["id"],
            "day": day.isoformat(),
            "model": payload["model"],
            "symbols": len(facts["symbols"]),
            "batches": len(facts["batches"]),
            "dissents": len(facts["dissents"]),
            "resolutions": len(facts["resolutions"]),
            "replaced": bool(existing),
            "advisory": ADVISORY,
        }
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
