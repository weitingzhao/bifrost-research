"""The nightly memory distillation (K6 — design Rev .96, Spec §20).

Reads the trader's own trail after the close and writes ``journal.memory``:
every memory measured, never invented — a value the sources cannot support
is absent, not guessed (the same honesty rule the lenses live by).

Sources and what v1 distils from each:

- **fills** (``raw_broker.executions_final``, in this same database): option
  fill pairs → the holding-period axis, the exit-style axis, and the
  weak-spot axis (the name that keeps costing). The account's book is the
  installation's, so every research user reads the same trading facts —
  §20.5 keys the *portrait*, and the persons behind the users share one book.
- **decisions** (``research.ai_draft``): how the Decision Inbox is being
  answered — decided vs left pending over the last 30 days.
- **visits** (``journal.visit``): the most-revisited names → a did-memory;
  rows cited as evidence are flagged and survive the 90-day trim (§20.4).
- **notes** (``journal.note``): a note linked to a symbol a memory is about
  is cited as evidence — and citing it locks it (§20.1,
  ``distilled_memory_id``).
- **threads**: registered as a source, distils nothing yet — mining prose
  needs a judge, which is not this heuristic's to invent. Named owed.

Contract mechanics (§20): Forget is a topic-level tombstone — a tombstoned
topic is never rewritten, however its evidence looks tomorrow. fading's end
is the archive (strength floor), never deletion — the portrait axes it backs
do not silently collapse. The risk axis needs the Trade side's gate history,
which this domain cannot read (D13) — absent, named here, not faked.

Idempotent per day: re-running upserts the same topics; a same-day re-run
keeps the morning's ``change`` verdict unless strength actually moved again.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Any

from psycopg2.extras import RealDictCursor

from bifrost_research.schema.schemas import (
    TABLE_JOURNAL_MEMORY,
    TABLE_JOURNAL_MEMORY_TOMBSTONE,
    TABLE_JOURNAL_NOTE,
    TABLE_JOURNAL_SOURCE_SETTING,
    TABLE_JOURNAL_VISIT,
)

logger = logging.getLogger(__name__)

SOURCES = ("notes", "fills", "decisions", "threads", "visits")
ARCHIVE_STRENGTH = 0.15
FADE_DELTA = 0.05
VISIT_KEEP_DAYS = 90
EVIDENCE_CAP = 6


@dataclass
class Candidate:
    """One distilled conclusion, before the store's change/tombstone rules."""

    topic: str
    kind: str  # did | said | tension
    text_md: str
    strength: float
    axis: str | None = None
    value: str = ""
    sub: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # Symbols this memory is about — notes linked to them become evidence.
    symbols: tuple[str, ...] = ()


# ── fills → closed positions ─────────────────────────────────────────────────


def pair_option_fills(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill rows → closed positions: open/close dates, days held, realized.

    A position is one contract_key's fills; closed when net quantity returns
    to zero. Buys are negative cash, sells positive, ×100 per contract — the
    same arithmetic the ledger's execution groups use.
    """
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if (r.get("sec_type") or "").upper() != "OPT":
            continue
        key = r.get("contract_key") or ""
        if key and r.get("exec_time") is not None:
            by_key[key].append(r)
    out: list[dict[str, Any]] = []
    for key, fills in by_key.items():
        fills.sort(key=lambda r: r["exec_time"])
        net = 0.0
        cash = 0.0
        opened: datetime | None = None
        short_open = False
        open_cash = 0.0
        for f in fills:
            qty = abs(float(f.get("quantity") or 0))
            price = float(f.get("price") or 0)
            buy = (f.get("side") or "").upper() in ("BUY", "BOT", "B")
            leg_cash = (-1 if buy else 1) * price * qty * 100
            if net == 0:
                # Opening fill of a cycle. Scale-ins later only add to cash;
                # open_cash stays the first leg's credit/debit (a heuristic).
                opened = f["exec_time"]
                short_open = not buy
                open_cash = leg_cash
            cash += leg_cash
            net += qty if buy else -qty
            if abs(net) < 1e-9 and opened is not None:
                closed = f["exec_time"]
                out.append(
                    {
                        "contract_key": key,
                        "symbol": (f.get("symbol") or "").split(" ")[0].upper(),
                        "opened": opened,
                        "closed": closed,
                        "days": max(0, (closed - opened).days),
                        "realized": cash,
                        "short_open": short_open,
                        "open_cash": open_cash,
                    }
                )
                cash = 0.0
                opened = None
    return out


def _ev(source: str, when: Any, text: str, route: str = "") -> dict[str, Any]:
    if isinstance(when, datetime):
        when = when.date().isoformat()
    elif isinstance(when, date):
        when = when.isoformat()
    return {"source": source, "date": str(when or ""), "text": text[:200], "route": route}


def _usd(amount: float) -> str:
    sign = "+" if amount >= 0 else "−"
    return f"{sign}${abs(round(amount))}"


# ── candidates from each source ──────────────────────────────────────────────


def candidates_from_pairs(pairs: list[dict[str, Any]]) -> list[Candidate]:
    out: list[Candidate] = []
    closed = [p for p in pairs if p.get("closed")]
    if len(closed) >= 5:
        med = int(median(p["days"] for p in closed))
        recent = sorted(closed, key=lambda p: p["closed"], reverse=True)[:3]
        out.append(
            Candidate(
                topic="axis-hold",
                kind="did",
                axis="hold",
                value=f"{med}d",
                sub=f"median · {len(closed)} closed",
                text_md=(
                    f"Holding period runs a median {med} days across "
                    f"{len(closed)} closed option positions."
                ),
                strength=min(1.0, len(closed) / 20),
                evidence=[
                    _ev("fills", p["closed"], f"{p['symbol']} {p['days']}d · {_usd(p['realized'])}")
                    for p in recent
                ],
            )
        )
    shorts = [p for p in closed if p["short_open"] and p["open_cash"] > 0]
    if len(shorts) >= 5:
        kept = [p for p in shorts if p["realized"] >= 0.5 * p["open_cash"]]
        share = len(kept) / len(shorts)
        out.append(
            Candidate(
                topic="axis-exit",
                kind="did",
                axis="exit",
                value="Half the credit" if share >= 0.6 else "Runs to the wire",
                sub=f"{len(kept)} of {len(shorts)} shorts kept ≥50% of credit",
                text_md=(
                    f"{len(kept)} of {len(shorts)} closed short positions kept at least "
                    "half the credit — exits lean early."
                    if share >= 0.6
                    else f"Only {len(kept)} of {len(shorts)} closed shorts kept half the "
                    "credit — exits run late."
                ),
                strength=min(1.0, len(shorts) / 15),
                evidence=[
                    _ev(
                        "fills",
                        p["closed"],
                        f"{p['symbol']} kept {round(100 * p['realized'] / p['open_cash'])}% of credit",
                    )
                    for p in sorted(shorts, key=lambda p: p["closed"], reverse=True)[:3]
                ],
            )
        )
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in closed:
        by_sym[p["symbol"]].append(p)
    losers = [
        (sym, sum(p["realized"] for p in ps), len(ps))
        for sym, ps in by_sym.items()
        if sym and len(ps) >= 3 and sum(p["realized"] for p in ps) < 0
    ]
    if losers:
        sym, total, n = min(losers, key=lambda t: t[1])
        out.append(
            Candidate(
                topic="axis-trigger",
                kind="tension",
                axis="trigger",
                value=sym,
                sub=f"{n} closed · {_usd(total)}",
                text_md=f"{sym} keeps costing: {n} closed positions net {_usd(total)}.",
                strength=min(1.0, n / 8),
                symbols=(sym,),
                evidence=[
                    _ev("fills", p["closed"], f"{sym} {_usd(p['realized'])}")
                    for p in sorted(by_sym[sym], key=lambda p: p["closed"], reverse=True)[:3]
                ],
            )
        )
    return out


def candidates_from_decisions(counts: dict[str, int], *, today: date) -> list[Candidate]:
    """ai_draft status counts (30d) → how the Inbox is being answered."""
    decided = counts.get("approved", 0) + counts.get("dismissed", 0)
    pending = counts.get("pending", 0)
    total = decided + pending + counts.get("expired", 0)
    if total < 8:
        return []
    return [
        Candidate(
            topic="inbox-answering",
            kind="did",
            text_md=(
                f"Decision Inbox last 30d: {decided} cards decided "
                f"({counts.get('approved', 0)} approved · {counts.get('dismissed', 0)} "
                f"dismissed), {pending} still pending."
            ),
            strength=min(1.0, decided / 40),
            evidence=[
                _ev(
                    "decisions",
                    today,
                    f"{total} cards in the 30d window",
                    "/research/loop/decisions",
                )
            ],
        )
    ]


def candidates_from_visits(rows: list[dict[str, Any]], *, today: date) -> list[Candidate]:
    """visit rows (30d) → where the attention actually goes."""
    by_sym: dict[str, int] = defaultdict(int)
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if sym:
            by_sym[sym] += 1
    top = [(s, n) for s, n in sorted(by_sym.items(), key=lambda kv: -kv[1])[:3] if n >= 5]
    if not top:
        return []
    names = " · ".join(f"{s} ×{n}" for s, n in top)
    return [
        Candidate(
            topic="visits-focus",
            kind="did",
            text_md=f"Attention last 30d sits on {names} — the names read most.",
            strength=min(1.0, sum(n for _, n in top) / 40),
            symbols=tuple(s for s, _ in top),
            evidence=[
                _ev("visits", today, f"{s} visited {n}× in 30d", "/research/symbol")
                for s, n in top
            ],
        )
    ]


# ── store mechanics ──────────────────────────────────────────────────────────


def resolve_change(
    prev: dict[str, Any] | None, strength: float, *, today: date | None = None
) -> tuple[str, bool]:
    """(change, archived) under §20.3 — fading's end is the archive."""
    archived = strength < ARCHIVE_STRENGTH
    if prev is None:
        return "new", archived
    old = float(prev.get("strength") or 0)
    if strength > old + FADE_DELTA:
        return "stronger", archived
    if strength < old - FADE_DELTA:
        return "fading", archived
    # Same-day re-run: keep the morning's verdict instead of flattening it.
    if today is not None and prev.get("last_seen") == today:
        return str(prev.get("change") or "steady"), archived
    return "steady", archived


def run_distill(conn: Any, *, today: date | None = None) -> dict[str, Any]:
    """One pass for every owner with any journal presence."""
    today = today or datetime.now().astimezone().date()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT DISTINCT owner_id FROM (
                SELECT owner_id FROM {TABLE_JOURNAL_NOTE}
                UNION SELECT owner_id FROM {TABLE_JOURNAL_VISIT}
                UNION SELECT owner_id FROM {TABLE_JOURNAL_MEMORY}
                UNION SELECT owner_id FROM {TABLE_JOURNAL_SOURCE_SETTING}
            ) o
            """
        )
        owners = [r["owner_id"] for r in cur.fetchall()]
        cur.execute(
            """
            SELECT contract_key, symbol, sec_type, side, quantity, price, exec_time
            FROM raw_broker.executions_final
            ORDER BY exec_time
            """
        )
        fills = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT status, count(*) AS n FROM research.ai_draft
            WHERE created_at >= now() - interval '30 days'
            GROUP BY status
            """
        )
        decision_counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
    pairs = pair_option_fills(fills)

    written = 0
    locked_notes = 0
    for owner in owners:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT source, enabled FROM {TABLE_JOURNAL_SOURCE_SETTING} WHERE owner_id = %s",
                (owner,),
            )
            enabled = {s: True for s in SOURCES}
            enabled.update({r["source"]: bool(r["enabled"]) for r in cur.fetchall()})
            cur.execute(
                f"""
                SELECT visit_id, route, symbol, at FROM {TABLE_JOURNAL_VISIT}
                WHERE owner_id = %s AND at >= now() - interval '30 days'
                """,
                (owner,),
            )
            visits = [dict(r) for r in cur.fetchall()]
            cur.execute(
                f"SELECT topic FROM {TABLE_JOURNAL_MEMORY_TOMBSTONE} WHERE owner_id = %s",
                (owner,),
            )
            tombstoned = {r["topic"] for r in cur.fetchall()}
            cur.execute(f"SELECT * FROM {TABLE_JOURNAL_MEMORY} WHERE owner_id = %s", (owner,))
            prev_by_topic = {r["topic"]: dict(r) for r in cur.fetchall()}

        cands: list[Candidate] = []
        if enabled["fills"]:
            cands += candidates_from_pairs(pairs)
        if enabled["decisions"]:
            cands += candidates_from_decisions(decision_counts, today=today)
        if enabled["visits"]:
            cands += candidates_from_visits(visits, today=today)
        # threads: registered, distils nothing yet — a judge's job, named owed.

        for c in cands:
            if c.topic in tombstoned:
                continue  # §20.2 — the conclusion itself is suppressed
            evidence = list(c.evidence)
            note_ids: list[str] = []
            if enabled["notes"] and c.symbols:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id, body_md, page_route, created_at FROM {TABLE_JOURNAL_NOTE}
                        WHERE owner_id = %s AND distilled_memory_id IS NULL
                          AND refs @> ANY(%s::jsonb[])
                        ORDER BY created_at DESC LIMIT 2
                        """,
                        (
                            owner,
                            [json.dumps([{"type": "sym", "id": s}]) for s in c.symbols],
                        ),
                    )
                    for n in cur.fetchall():
                        evidence.append(
                            _ev("notes", n["created_at"], n["body_md"], n["page_route"] or "")
                        )
                        note_ids.append(str(n["id"]))
            change, archived = resolve_change(
                prev_by_topic.get(c.topic), c.strength, today=today
            )
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    INSERT INTO {TABLE_JOURNAL_MEMORY}
                        (owner_id, topic, kind, axis, value, sub, text_md, evidence,
                         strength, change, archived, first_seen, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                    ON CONFLICT (owner_id, topic) DO UPDATE SET
                        kind = EXCLUDED.kind, axis = EXCLUDED.axis,
                        value = EXCLUDED.value, sub = EXCLUDED.sub,
                        text_md = EXCLUDED.text_md, evidence = EXCLUDED.evidence,
                        strength = EXCLUDED.strength, change = EXCLUDED.change,
                        archived = EXCLUDED.archived, last_seen = EXCLUDED.last_seen,
                        updated_at = now()
                    RETURNING mem_no
                    """,
                    (
                        owner,
                        c.topic,
                        c.kind,
                        c.axis,
                        c.value,
                        c.sub,
                        c.text_md,
                        json.dumps(evidence[:EVIDENCE_CAP]),
                        c.strength,
                        change,
                        archived,
                        today,
                        today,
                    ),
                )
                mem_no = cur.fetchone()["mem_no"]
                if note_ids:
                    # §20.1 — citing a note as evidence locks it.
                    cur.execute(
                        f"""
                        UPDATE {TABLE_JOURNAL_NOTE}
                        SET distilled_memory_id = %s
                        WHERE id = ANY(%s::uuid[]) AND distilled_memory_id IS NULL
                        """,
                        (f"M-{mem_no}", note_ids),
                    )
                    locked_notes += cur.rowcount
                if c.topic == "visits-focus" and c.symbols and visits:
                    # §20.4 — flag the latest cited visit per symbol so the
                    # evidence outlives the 90-day trim.
                    latest = {}
                    for v in visits:
                        s = (v.get("symbol") or "").upper()
                        if s in c.symbols:
                            latest[s] = max(latest.get(s, 0), int(v["visit_id"]))
                    if latest:
                        cur.execute(
                            f"UPDATE {TABLE_JOURNAL_VISIT} SET evidence = true "
                            "WHERE visit_id = ANY(%s)",
                            (list(latest.values()),),
                        )
            written += 1

    # §20.4 — visits roll 90 days; evidence-flagged rows ride with their memory.
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {TABLE_JOURNAL_VISIT}
            WHERE at < now() - interval '{VISIT_KEEP_DAYS} days' AND NOT evidence
            """
        )
        trimmed = cur.rowcount
    conn.commit()
    result = {
        "owners": len(owners),
        "pairs": len(pairs),
        "memories_written": written,
        "notes_locked": locked_notes,
        "visits_trimmed": trimmed,
    }
    logger.info("journal distill: %s", result)
    return result
