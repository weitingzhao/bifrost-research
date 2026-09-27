"""journal.memory / visit / hint / source — reads and owner writes (K6, §20).

The distiller (``engines/journal_distill``) is the only writer of memory
*content*; everything here is what the shell needs: the You page's portrait,
Forget (§20.2 — a topic tombstone, so the same evidence never re-produces the
conclusion), the visits beacon, the Plans hint with its dismissal counter
(§20.6 — counts live in this store; past a threshold the prompt quiets), and
the Journal's Day view (the raw trail, read from the stores it happened in).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from psycopg2.extras import RealDictCursor

from bifrost_research.schema.schemas import (
    TABLE_JOURNAL_HINT_DISMISSAL,
    TABLE_JOURNAL_MEMORY,
    TABLE_JOURNAL_MEMORY_TOMBSTONE,
    TABLE_JOURNAL_NOTE,
    TABLE_JOURNAL_SOURCE_SETTING,
    TABLE_JOURNAL_VISIT,
)

DAY_TZ = "America/New_York"  # the Journal's day is the trading day
HINT_QUIET_AT = 3  # §20.6 — dismissals at which a hint silences itself
AXIS_LABELS = {"hold": "Holding period", "exit": "Exit", "risk": "Risk", "trigger": "Weak spot"}

_MEM_ID = re.compile(r"^M-(\d+)$")


def _mem_row(r: dict[str, Any]) -> dict[str, Any]:
    ev = r.get("evidence")
    return {
        "id": f"M-{r['mem_no']}",
        "topic": r["topic"],
        "kind": r["kind"],
        "axis": r.get("axis"),
        "value": r.get("value") or "",
        "sub": r.get("sub") or "",
        "text": r.get("text_md") or "",
        "evidence": ev if isinstance(ev, list) else [],
        "strength": float(r.get("strength") or 0),
        "change": r.get("change") or "steady",
        "archived": bool(r.get("archived")),
        "first_seen": r["first_seen"].isoformat() if r.get("first_seen") else None,
        "last_seen": r["last_seen"].isoformat() if r.get("last_seen") else None,
    }


def list_memories(
    conn: Any, *, owner_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    clause = "" if include_archived else "AND NOT archived"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT * FROM {TABLE_JOURNAL_MEMORY}
            WHERE owner_id = %s {clause}
            ORDER BY strength DESC, last_seen DESC
            """,
            (owner_id,),
        )
        rows = cur.fetchall() or []
    return [_mem_row(dict(r)) for r in rows]


def portrait_axes(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The four portrait axes from memory rows — archived rows still back them
    (§20.3: fading ends in the archive precisely so the portrait holds)."""
    out: list[dict[str, Any]] = []
    for axis, label in AXIS_LABELS.items():
        backing = [m for m in memories if m.get("axis") == axis]
        if not backing:
            continue  # absent, not invented — the risk axis is owed (D13)
        lead = max(backing, key=lambda m: m["strength"])
        out.append(
            {
                "id": axis,
                "label": label,
                "value": lead["value"] or lead["text"][:60],
                "sub": lead["sub"],
                "backs": [m["id"] for m in backing],
                "warn": axis == "trigger",
            }
        )
    return out


def forget_memory(conn: Any, *, owner_id: str, mem_id: str) -> str | None:
    """§20.2 — Forget suppresses the CONCLUSION: tombstone the topic, delete
    the row, release the notes it held (the lock has no referent left).
    Returns the tombstoned topic, or None when the id isn't this owner's."""
    m = _MEM_ID.match(mem_id or "")
    if not m:
        return None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT topic FROM {TABLE_JOURNAL_MEMORY} WHERE owner_id = %s AND mem_no = %s",
            (owner_id, int(m.group(1))),
        )
        row = cur.fetchone()
        if row is None:
            return None
        topic = row["topic"]
        cur.execute(
            f"""
            INSERT INTO {TABLE_JOURNAL_MEMORY_TOMBSTONE} (owner_id, topic)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
            """,
            (owner_id, topic),
        )
        cur.execute(
            f"DELETE FROM {TABLE_JOURNAL_MEMORY} WHERE owner_id = %s AND topic = %s",
            (owner_id, topic),
        )
        cur.execute(
            f"""
            UPDATE {TABLE_JOURNAL_NOTE} SET distilled_memory_id = NULL
            WHERE owner_id = %s AND distilled_memory_id = %s
            """,
            (owner_id, mem_id),
        )
    conn.commit()
    return topic


# ── sources (§20 — turning one off stops future distills from it) ───────────


def list_sources(conn: Any, *, owner_id: str, all_sources: tuple[str, ...]) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT source, enabled FROM {TABLE_JOURNAL_SOURCE_SETTING} WHERE owner_id = %s",
            (owner_id,),
        )
        saved = {r["source"]: bool(r["enabled"]) for r in cur.fetchall()}
    return [{"source": s, "enabled": saved.get(s, True)} for s in all_sources]


def set_source(conn: Any, *, owner_id: str, source: str, enabled: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_JOURNAL_SOURCE_SETTING} (owner_id, source, enabled)
            VALUES (%s, %s, %s)
            ON CONFLICT (owner_id, source) DO UPDATE SET enabled = EXCLUDED.enabled
            """,
            (owner_id, source, enabled),
        )
    conn.commit()


# ── visits beacon (§20.4 — raw rows roll 90 days; the distiller trims) ───────


def insert_visit(conn: Any, *, owner_id: str, route: str, symbol: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_JOURNAL_VISIT} (owner_id, route, symbol) VALUES (%s, %s, %s)",
            (owner_id, route, (symbol or "").upper()),
        )
    conn.commit()


# ── hints (§20.6 — dismissal counts live here; the You page shows them) ──────


def hint_counts(conn: Any, *, owner_id: str) -> dict[str, int]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT hint_topic, count FROM {TABLE_JOURNAL_HINT_DISMISSAL} WHERE owner_id = %s",
            (owner_id,),
        )
        return {r["hint_topic"]: int(r["count"]) for r in cur.fetchall()}


def dismiss_hint(conn: Any, *, owner_id: str, topic: str) -> int:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_JOURNAL_HINT_DISMISSAL} (owner_id, hint_topic, count, last_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (owner_id, hint_topic)
            DO UPDATE SET count = {TABLE_JOURNAL_HINT_DISMISSAL}.count + 1, last_at = now()
            RETURNING count
            """,
            (owner_id, topic),
        )
        return int(cur.fetchone()["count"])


def hint_for_symbol(conn: Any, *, owner_id: str, symbol: str) -> dict[str, Any] | None:
    """The Plans page's memory hint: a tension/weak-spot memory about this
    name. Quieted (§20.6) once dismissed HINT_QUIET_AT times — still returned,
    marked, so the You page can show what went quiet."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    mem: dict[str, Any] | None = None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT * FROM {TABLE_JOURNAL_MEMORY}
            WHERE owner_id = %s AND NOT archived
              AND value = %s AND axis = 'trigger'
            ORDER BY strength DESC LIMIT 1
            """,
            (owner_id, sym),
        )
        row = cur.fetchone()
        if row is not None:
            mem = _mem_row(dict(row))
    if mem is None:
        # The prototype's second trigger: a print inside 3 days, spoken by the
        # earnings weak-spot memory when the book has earned one. The estimate
        # is the 8-K rule's own (repositories.earnings_filings.expected_next).
        days = _days_to_print(conn, sym)
        if days is not None and 0 <= days <= 3:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT * FROM {TABLE_JOURNAL_MEMORY}
                    WHERE owner_id = %s AND NOT archived
                      AND topic = 'axis-trigger-earnings'
                    LIMIT 1
                    """,
                    (owner_id,),
                )
                row = cur.fetchone()
            if row is not None:
                mem = _mem_row(dict(row))
                mem["text"] = f"{mem['text']} {sym} reports in {days} day{'s' if days != 1 else ''}."
    # The first-visit-today trigger stays owed: its memory topic needs
    # visits×fills history, and visits only started accumulating 2026-09-27.
    if mem is None:
        return None
    count = hint_counts(conn, owner_id=owner_id).get(mem["topic"], 0)
    return {**mem, "dismissals": count, "quiet": count >= HINT_QUIET_AT}


def _days_to_print(conn: Any, symbol: str) -> int | None:
    """days_away of the estimated next print, by the 8-K rule; None = no read."""
    try:
        from bifrost_research.repositories.earnings_filings import (
            expected_next,
            fetch_item_202,
            split_releases,
        )

        releases = split_releases(fetch_item_202(conn, symbol))[0]
        est = expected_next(releases, as_of=date.today())
    except Exception:  # noqa: BLE001 — a hint must never take the sheet down
        return None
    if not est:
        return None
    days = est.get("days_away")
    return int(days) if isinstance(days, int) else None


# ── the Journal's Day view (the raw trail; prose summary is owed) ────────────


def day_view(conn: Any, *, owner_id: str, day: date) -> dict[str, Any]:
    """Everything of one trading day, read from the stores it happened in.

    Predicates convert to the New York day per row — non-sargable, and fine:
    every table here is one owner's trail (the fills table is the whole book,
    482 rows today). Not a lens; do not copy this shape onto raw_market.*.
    """
    traces: list[dict[str, Any]] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT body_md, page_route, page_label, created_at
            FROM {TABLE_JOURNAL_NOTE}
            WHERE owner_id = %s AND (created_at AT TIME ZONE %s)::date = %s
            ORDER BY created_at
            """,
            (owner_id, DAY_TZ, day),
        )
        for r in cur.fetchall():
            traces.append(
                {
                    "at": r["created_at"].isoformat(),
                    "kind": "note",
                    "text": (r["body_md"] or "")[:200],
                    "where": r["page_label"] or r["page_route"] or "",
                    "to": r["page_route"] or "/research/journal?view=notes",
                }
            )
        cur.execute(
            f"""
            SELECT route, symbol, count(*) AS n, min(at) AS first_at
            FROM {TABLE_JOURNAL_VISIT}
            WHERE owner_id = %s AND (at AT TIME ZONE %s)::date = %s
            GROUP BY route, symbol ORDER BY first_at
            """,
            (owner_id, DAY_TZ, day),
        )
        for r in cur.fetchall():
            sym = r["symbol"] or ""
            traces.append(
                {
                    "at": r["first_at"].isoformat(),
                    "kind": "visit",
                    "text": (f"{sym} · " if sym else "") + f"{r['route']} ×{r['n']}",
                    "where": r["route"],
                    "to": r["route"] + (f"?symbol={sym}" if sym else ""),
                }
            )
        cur.execute(
            """
            SELECT exec_time, symbol, sec_type, side, quantity, price
            FROM raw_broker.executions_final
            WHERE (exec_time AT TIME ZONE %s)::date = %s
            ORDER BY exec_time
            """,
            (DAY_TZ, day),
        )
        for r in cur.fetchall():
            qty = abs(float(r["quantity"] or 0))
            traces.append(
                {
                    "at": r["exec_time"].isoformat(),
                    "kind": "fill",
                    "text": f"{(r['side'] or '').upper()} {r['symbol']} ×{qty:g} @ {float(r['price'] or 0):g}",
                    "where": "Trade › Ledger",
                    "to": "/portfolio/ledger",
                }
            )
        cur.execute(
            """
            SELECT status, count(*) AS n FROM research.ai_draft
            WHERE (created_at AT TIME ZONE %s)::date = %s
            GROUP BY status
            """,
            (DAY_TZ, day),
        )
        drafts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
        if drafts:
            total = sum(drafts.values())
            parts = " · ".join(f"{n} {s}" for s, n in sorted(drafts.items(), key=lambda kv: -kv[1]))
            traces.append(
                {
                    "at": f"{day.isoformat()}T00:00:00",
                    "kind": "decision",
                    "text": f"{total} Inbox cards this day — {parts}",
                    "where": "Review › Decision Inbox",
                    "to": "/research/loop/decisions",
                }
            )
        cur.execute(
            """
            SELECT id, title, created_at FROM research.copilot_session
            WHERE owner_id = %s AND (created_at AT TIME ZONE %s)::date = %s
            ORDER BY created_at
            """,
            (owner_id, DAY_TZ, day),
        )
        for r in cur.fetchall():
            traces.append(
                {
                    "at": r["created_at"].isoformat(),
                    "kind": "thread",
                    "text": r["title"] or "Copilot thread",
                    "where": "Copilot",
                    "to": "/research/copilot",
                }
            )
        cur.execute(
            f"""
            SELECT mem_no, change, topic FROM {TABLE_JOURNAL_MEMORY}
            WHERE owner_id = %s AND last_seen = %s
            ORDER BY strength DESC
            """,
            (owner_id, day),
        )
        changes = [[f"M-{r['mem_no']}", r["change"], r["topic"]] for r in cur.fetchall()]
    traces.sort(key=lambda t: t["at"])
    return {"date": day.isoformat(), "traces": traces, "changes": changes}


def week_summary(conn: Any, *, owner_id: str, today: date) -> dict[str, Any]:
    monday = today - timedelta(days=today.weekday())
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT count(*) AS n FROM {TABLE_JOURNAL_MEMORY}
            WHERE owner_id = %s AND last_seen >= %s AND change <> 'steady'
            """,
            (owner_id, monday),
        )
        moved = int(cur.fetchone()["n"])
        cur.execute(
            f"""
            SELECT count(*) AS n FROM {TABLE_JOURNAL_MEMORY_TOMBSTONE}
            WHERE owner_id = %s AND created_at >= %s
            """,
            (owner_id, monday),
        )
        forgot = int(cur.fetchone()["n"])
    return {
        "range": f"{monday.isoformat()} → {today.isoformat()}",
        "moved": moved,
        "forgot": forgot,
    }


__all__ = [
    "DAY_TZ",
    "HINT_QUIET_AT",
    "day_view",
    "dismiss_hint",
    "forget_memory",
    "hint_counts",
    "hint_for_symbol",
    "insert_visit",
    "list_memories",
    "list_sources",
    "portrait_axes",
    "set_source",
    "week_summary",
]
