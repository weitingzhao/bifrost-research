"""The option universe: three tiers, two rules, no hand list.

Until now the set of symbols with option surfaces was whatever the Plugin had
ingested — the Owner's watchlist plus three benchmarks, twenty-seven names —
while the stock side ran on a rule (`dim_universe`, 5,376). A ratio of two
hundred between the two halves of one funnel. This module makes the option
side a rule too, and writes it to `research.option_universe` for the Plugin
to read (what to enumerate) and for Research's engines (what to compute).

Tiers, in precedence order:

- resident — holdings, the Owner's watchlist, the IV-radar benchmarks, and the
  index option roots the Plugin already ingests. Never leaves. 24 months of
  history. The Plugin snapshots resident chains whole, so this tier is the
  expensive one: it must stay the names someone asked for.
- core — common stock in `dim_universe` whose 20-session average dollar
  volume clears CORE_ENTER_USD; leaves only below CORE_EXIT_USD. The
  hysteresis is what keeps a name's percentile history from fragmenting at
  the threshold. 24 months.
- edge — the stock screen's survivors (SEPA SETUP / PIVOT at or above
  EDGE_MIN_SCORE) not already above. Enters on first appearance with 12
  months of history, enough for a 252-day IV rank; stays EDGE_RETENTION_DAYS
  after its last appearance so its 20-day hits settle and the 60-session skew
  gate can mature.

Optionability is not predicted here — `raw_market.ticker` has no such flag.
A symbol with no contracts drops out at the Plugin's enumeration, which is
the only place that can know.

Writes `research.option_universe` only. D10 BLOCKED.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import TABLE_RESEARCH_OPTION_UNIVERSE

logger = logging.getLogger(__name__)

RESIDENT_BENCHMARKS: tuple[str, ...] = ("SPY", "QQQ", "IWM")
#: Cash-settled US index option roots. A market fact, not a coverage choice: it
#: says which underlyings are indices, and only already-ingested ones count
#: (see load_resident), so a root the Plugin does not fetch has no effect.
INDEX_OPTION_ROOTS: frozenset[str] = frozenset(
    {"SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "RUTW", "MRUT", "VIX", "VIXW", "DJX", "OEX", "XEO"}
)
CORE_ENTER_USD = 2.0e8
CORE_EXIT_USD = 1.2e8  # 60% of the entry floor
LIQUIDITY_WINDOW_DAYS = 30  # calendar days ≈ 20 sessions
EDGE_MIN_SCORE = 70.0
EDGE_PATHS: tuple[str, ...] = ("SETUP", "PIVOT")
EDGE_RETENTION_DAYS = 130  # calendar days ≈ 90 sessions
CORE_HISTORY_MONTHS = 24
EDGE_HISTORY_MONTHS = 12

TIER_RANK = {"resident": 0, "core": 1, "edge": 2}


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ── inputs ────────────────────────────────────────────────────────────────


def load_existing(conn: Any) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT symbol, tier, entered_on, last_seen, history_months, reason "
            f"FROM {TABLE_RESEARCH_OPTION_UNIVERSE}"
        )
        rows = cur.fetchall() or []
    return {
        str(r[0]).upper(): {
            "tier": r[1],
            "entered_on": r[2],
            "last_seen": r[3],
            "history_months": int(r[4]),
            "reason": r[5],
        }
        for r in rows
    }


def load_resident(conn: Any) -> dict[str, str]:
    """symbol → reason: benchmarks, the Plugin's watchlist union, and the index roots already ingested.

    The watchlist union is stocks only (`sec_type = 'STK'`), so the index
    underlyings the Owner watches — SPX, SPXW — never arrive through it. They
    are in `raw_market.option_contract` because the Plugin was told to fetch
    them, so an ingested *index root* is grandfathered in.

    Only index roots. Until 0.102.0 every ingested underlying qualified, and
    that rule absorbs its own output: the Plugin ingests every name in this
    table, so each refresh made the names it had just collected resident. On
    2026-09-11 the first refresh to succeed since 09-05 moved 548 names from
    core and edge into resident; the Plugin snapshots resident chains whole,
    and a session's snapshot would have gone from ~187,000 contracts to
    716,787 — ~18 GB of 90-session retention to ~130 GB.
    """
    out: dict[str, str] = {s: "benchmark" for s in RESIDENT_BENCHMARKS}
    for sql, params, reason in (
        ("SELECT symbol FROM ops_jobs.watchlist_cache", None, "watchlist"),
        (
            "SELECT DISTINCT underlying FROM raw_market.option_contract WHERE underlying = ANY(%s)",
            (sorted(INDEX_OPTION_ROOTS),),
            "ingested",
        ),
    ):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                for (sym,) in cur.fetchall() or []:
                    out.setdefault(str(sym).strip().upper(), reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resident source unreadable (%s): %s", reason, exc)
            conn.rollback()
    return out


def load_liquidity(conn: Any, as_of: date) -> dict[str, float]:
    """symbol → average daily dollar volume over the window, common stock only."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.symbol, AVG(d.close * d.volume)
            FROM raw_market.stock_daily d
            JOIN dw_stock.dim_universe u ON u.symbol = d.symbol
            WHERE d.bar_date > %s::date - %s::int AND d.bar_date <= %s::date
            GROUP BY d.symbol
            """,
            (as_of, LIQUIDITY_WINDOW_DAYS, as_of),
        )
        return {str(s).upper(): float(v or 0.0) for s, v in (cur.fetchall() or [])}


def load_edge_candidates(conn: Any) -> set[str]:
    """Today's screen survivors: the latest SEPA session, SETUP / PIVOT, score at or above the floor."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol FROM features.stock_signal_sepa_daily
            WHERE trade_date = (SELECT MAX(trade_date) FROM features.stock_signal_sepa_daily)
              AND path = ANY(%s) AND sepa_score >= %s
            """,
            (list(EDGE_PATHS), EDGE_MIN_SCORE),
        )
        return {str(s).upper() for (s,) in (cur.fetchall() or [])}


# ── the rule ──────────────────────────────────────────────────────────────


def _grandfathered(prev: dict[str, Any] | None) -> bool:
    """Held as resident only by the old every-ingested-name route (see load_resident)."""
    return prev is not None and prev.get("tier") == "resident" and prev.get("reason") == "ingested"


def build_universe(
    *,
    as_of: date,
    existing: dict[str, dict[str, Any]],
    resident: dict[str, str],
    liquidity: dict[str, float],
    edge_candidates: set[str],
) -> dict[str, dict[str, Any]]:
    """Pure: the next state of the table from its inputs."""
    out: dict[str, dict[str, Any]] = {}

    def place(symbol: str, tier: str, reason: str, months: int, *, seen: bool) -> None:
        prev = existing.get(symbol)
        keep_entry = prev is not None and prev["tier"] == tier
        out[symbol] = {
            "tier": tier,
            "entered_on": prev["entered_on"] if keep_entry else as_of,
            "last_seen": as_of if seen else (prev["last_seen"] if prev else as_of),
            "history_months": months,
            "reason": reason,
        }

    for sym, reason in resident.items():
        place(sym, "resident", reason, CORE_HISTORY_MONTHS, seen=True)

    for sym, dv in liquidity.items():
        if sym in out:
            continue
        # A name the old ingested route promoted left core or edge to get there;
        # which one was not recorded, so it keeps core's exit floor on the way
        # back rather than having to clear the entry floor again.
        was_core = existing.get(sym, {}).get("tier") == "core" or _grandfathered(existing.get(sym))
        if dv >= CORE_ENTER_USD or (was_core and dv >= CORE_EXIT_USD):
            place(sym, "core", f"dollar_volume>={CORE_ENTER_USD:.0e}", CORE_HISTORY_MONTHS, seen=True)

    for sym in edge_candidates:
        if sym not in out:
            place(sym, "edge", f"sepa>={EDGE_MIN_SCORE:.0f}", EDGE_HISTORY_MONTHS, seen=True)

    # Edge names not seen today stay until their retention runs out. A core
    # name that fell below the exit floor, or a resident name gone from the
    # watchlist, is simply not re-placed and leaves. A name only the old
    # ingested route held steps down to edge instead: dropping it would
    # fragment its IV history over a labelling fault, not a change in the name.
    cutoff = as_of - timedelta(days=EDGE_RETENTION_DAYS)
    for sym, prev in existing.items():
        if sym in out or prev["last_seen"] < cutoff:
            continue
        if prev["tier"] == "edge":
            out[sym] = dict(prev)
        elif _grandfathered(prev):
            place(sym, "edge", "stepped-down:ingested", EDGE_HISTORY_MONTHS, seen=False)
    return out


# ── the write ─────────────────────────────────────────────────────────────


def write_universe(conn: Any, rows: dict[str, dict[str, Any]], *, existing: dict[str, dict[str, Any]]) -> dict[str, int]:
    gone = sorted(set(existing) - set(rows))
    with conn.cursor() as cur:
        if gone:
            cur.execute(f"DELETE FROM {TABLE_RESEARCH_OPTION_UNIVERSE} WHERE symbol = ANY(%s)", (gone,))
        cur.executemany(
            f"""
            INSERT INTO {TABLE_RESEARCH_OPTION_UNIVERSE}
                (symbol, tier, entered_on, last_seen, history_months, reason, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                tier = EXCLUDED.tier, entered_on = EXCLUDED.entered_on,
                last_seen = EXCLUDED.last_seen, history_months = EXCLUDED.history_months,
                reason = EXCLUDED.reason, updated_at = now()
            """,
            [
                (s, r["tier"], r["entered_on"], r["last_seen"], r["history_months"], r["reason"])
                for s, r in sorted(rows.items())
            ],
        )
    conn.commit()
    return {"written": len(rows), "removed": len(gone)}


def run(*, as_of: date | None = None) -> dict[str, Any]:
    day = as_of or _today()
    conn = connect()
    try:
        # The liquidity scan walks a month of bars for every listed name; the
        # role's default statement timeout is sized for page reads, not this.
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")
        existing = load_existing(conn)
        resident = load_resident(conn)
        liquidity = load_liquidity(conn, day)
        edge = load_edge_candidates(conn)
        rows = build_universe(
            as_of=day, existing=existing, resident=resident, liquidity=liquidity, edge_candidates=edge
        )
        stats = write_universe(conn, rows, existing=existing)
    finally:
        conn.close()
    by_tier = {t: sum(1 for r in rows.values() if r["tier"] == t) for t in TIER_RANK}
    entered = sorted(s for s in rows if s not in existing)
    return {
        "engine": "option_universe",
        "as_of": day.isoformat(),
        **stats,
        "by_tier": by_tier,
        "entered": entered[:50],
        "entered_count": len(entered),
        "core_enter_usd": CORE_ENTER_USD,
        "core_exit_usd": CORE_EXIT_USD,
    }
