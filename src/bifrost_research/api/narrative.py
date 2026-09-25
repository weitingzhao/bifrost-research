"""Narrative lens API — GET /research/narrative.

The deterministic narrative readings over the SEC text the market-data plugin
ingests (`raw_market.sec_8k_filing`, `sec_8k_disclosure`, `sec_10k_section` —
read-only here, D13), each beside what the measured lenses say about the same
name (`features.stock_signal_scan_daily`), never blended into it (Vision §9.2).

`sources` answers "what is read": how many filings, from when, how much of it
the vendor classified, which 10-K sections are in. The model readings those
sections would feed are not produced here — see `lenses/narrative.py`.

`?symbol=` narrows the window to one name (the Symbol page's Narrative panel)
and adds `symbol_coverage`: that name's 8-K rows on file, all time. An empty
window then reads two ways the caller must not confuse — no filing this week
from a name the feed carries, or a name the feed has never carried.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from bifrost_research.db.conn import connect
from bifrost_research.lenses.narrative import sec_item_tags, vendor_tag
from bifrost_research.repositories.earnings_filings import fetch_item_202, split_releases

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/narrative", tags=["research-narrative"])

# The item headings sit near the top of the body; the longest bodies run to
# 240 KB of exhibit tables, which nothing on the page reads.
BODY_CHARS = 60_000


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _connect_or_503() -> Any:
    try:
        return connect()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc


def _today_et() -> Any:
    """Filings are dated by the SEC's calendar, which is New York's."""
    return datetime.now(ZoneInfo("America/New_York")).date()


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


@router.get("")
def narrative(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(400, ge=1, le=2000),
    symbol: str | None = Query(None, min_length=1, max_length=16),
) -> dict[str, Any]:
    today = _today_et()
    cutoff = today - timedelta(days=days)
    # The plugin stores tickers stripped and upper-cased; normalise the input,
    # never the column, so the (symbol, …) index is still usable.
    sym = symbol.strip().upper() if symbol else None
    only_sym = " AND symbol = %s" if sym else ""
    sym_args: tuple[Any, ...] = (sym,) if sym else ()
    coverage: dict[str, Any] | None = None
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(filing_date), MAX(filing_date), MAX(fetched_at)
                FROM raw_market.sec_8k_filing
                """
            )
            n_filings, n_names, first_filed, last_filed, fetched_at = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT accession_number, symbol FROM raw_market.sec_8k_disclosure
                ) d
                """
            )
            n_classified = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT section, COUNT(*), COUNT(DISTINCT symbol), MAX(period_end)
                FROM raw_market.sec_10k_section
                GROUP BY section
                ORDER BY section
                """
            )
            tenk = [
                {"section": s, "filings": int(n), "names": int(m), "latest_period": _iso(p)}
                for s, n, m, p in cur.fetchall()
            ]

            if sym:
                cur.execute(
                    """
                    SELECT COUNT(*), MIN(filing_date), MAX(filing_date)
                    FROM raw_market.sec_8k_filing
                    WHERE symbol = %s
                    """,
                    (sym,),
                )
                n_sym, first_sym, last_sym = cur.fetchone()
                coverage = {
                    "symbol": sym,
                    "filings": int(n_sym or 0),
                    "first_filed": _iso(first_sym),
                    "last_filed": _iso(last_sym),
                }

            cur.execute(
                """
                SELECT accession_number, symbol, filing_date, items, LEFT(items_text, %s), filing_url
                FROM raw_market.sec_8k_filing
                WHERE filing_date >= %s"""
                + only_sym
                + """
                ORDER BY filing_date DESC, symbol
                LIMIT %s
                """,
                (BODY_CHARS, cutoff, *sym_args, limit + 1),
            )
            filings = [
                {
                    "accession_number": a,
                    "symbol": s,
                    "filing_date": fd,
                    "items": items,
                    "items_text": body,
                    "filing_url": url,
                }
                for a, s, fd, items, body, url in cur.fetchall()
            ]
            truncated = len(filings) > limit
            filings = filings[:limit]

            cur.execute(
                """
                SELECT DISTINCT ON (accession_number, symbol, tertiary_category)
                       accession_number, symbol, filing_date, filing_url,
                       primary_category, secondary_category, tertiary_category, supporting_text
                FROM raw_market.sec_8k_disclosure
                WHERE filing_date >= %s"""
                + only_sym
                + """
                ORDER BY accession_number, symbol, tertiary_category, fetched_at DESC
                """,
                (cutoff, *sym_args),
            )
            cols = (
                "accession_number",
                "symbol",
                "filing_date",
                "filing_url",
                "primary_category",
                "secondary_category",
                "tertiary_category",
                "supporting_text",
            )
            kept = {(f["accession_number"], f["symbol"]) for f in filings}
            disclosures = [
                dict(zip(cols, r))
                for r in cur.fetchall()
                if (r[0], r[1]) in kept
            ]

            tags = [t for f in filings for t in sec_item_tags(f)] + [vendor_tag(d) for d in disclosures]
            symbols = sorted({t["symbol"] for t in tags})

            measured: dict[str, dict[str, Any]] = {}
            if symbols:
                cur.execute(
                    """
                    SELECT DISTINCT ON (symbol) symbol, trade_date, composite_score, iv_rank_1y
                    FROM features.stock_signal_scan_daily
                    WHERE symbol = ANY(%s) AND trade_date >= %s
                    ORDER BY symbol, trade_date DESC
                    """,
                    (symbols, today - timedelta(days=10)),
                )
                for sym, td, comp, ivr in cur.fetchall():
                    measured[sym] = {
                        "trade_date": _iso(td),
                        "composite": float(comp) if comp is not None else None,
                        "iv_rank": float(ivr) if ivr is not None else None,
                    }
    finally:
        conn.close()

    tags.sort(key=lambda t: (t["filing_date"], t["symbol"]), reverse=True)
    for t in tags:
        t["filing_date"] = _iso(t["filing_date"])
        t["measured"] = measured.get(t["symbol"])

    return _ok(
        {
            "as_of": _iso(fetched_at),
            "window_days": days,
            "truncated": truncated,
            "sources": {
                "filings_8k": {
                    "filings": int(n_filings or 0),
                    "names": int(n_names or 0),
                    "first_filed": _iso(first_filed),
                    "last_filed": _iso(last_filed),
                },
                "vendor_classified": {
                    "filings": n_classified,
                    "share": (n_classified / n_filings) if n_filings else None,
                },
                "tenk": tenk,
            },
            "symbol_coverage": coverage,
            "tags": tags,
            "count": len(tags),
        }
    )


@router.get("/earnings")
def earnings_dates(symbol: str = Query(..., min_length=1, max_length=16)) -> dict[str, Any]:
    """Dates this name filed an 8-K carrying Item 2.02 (results of operations) —
    the earnings dates the feed can vouch for, all of them — less the 2.02 filings
    that are not results releases (``repositories.earnings_filings``: Tesla's
    delivery reports and the like), which come back as ``set_aside`` (0.123.0).

    The History page reads these so an IV30 spike on an earnings print is called an
    event rather than a store fault (research 0.119.0). The feed carries the
    plugin's names only; ``filings`` says how many 8-Ks this name has on file at
    all, so an empty ``dates`` from a name the feed never carried is not read as
    "no earnings".
    """
    sym = symbol.strip().upper()
    conn = _connect_or_503()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), MIN(filing_date), MAX(filing_date)
                FROM raw_market.sec_8k_filing
                WHERE symbol = %s
                """,
                (sym,),
            )
            n, first_filed, last_filed = cur.fetchone()
        kept, set_aside = split_releases(fetch_item_202(conn, sym)) if n else ([], [])
        dates = [_iso(d) for d in kept]
    except Exception as exc:
        logger.exception("narrative/earnings failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        conn.close()
    return _ok(
        {
            "symbol": sym,
            "dates": dates,
            "set_aside": set_aside,
            "filings": int(n or 0),
            "first_filed": _iso(first_filed),
            "last_filed": _iso(last_filed),
        }
    )
