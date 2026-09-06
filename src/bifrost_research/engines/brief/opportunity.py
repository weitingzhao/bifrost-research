"""The Daily Brief's Opportunity card and sentiment wording — research-loop-automation A4.

Split from synth.py so the rule can be read on its own: the opportunity is the
symbol's own setup; a market-wide symbol reads the market's best setup; a single
name without a setup says so instead of borrowing another ticker's.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# Symbols whose brief is about the market, so the market's best setup is the opportunity.
MARKET_WIDE_SYMBOLS = frozenset({"SPX", "SPY", "QQQ", "IWM", "NDX", "RUT", "DIA", "VIX"})
TAPE_SOURCE = "option_trades_tape"

SEPA_ROUTE = "/research/sepa-daily-core"
MOMENTUM_ROUTE = "/research/momentum-radar"


def _sepa_pick(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """SETUP before PIVOT; anything else is not an opportunity."""
    setup_first = next((r for r in rows if r.get("path") == "SETUP"), None)
    pivot_first = next((r for r in rows if r.get("path") == "PIVOT"), None)
    return setup_first or pivot_first


def _sepa_text(row: dict[str, Any]) -> str:
    score = row.get("sepa_score")
    tail = f" · score {float(score):.0f}" if score is not None else ""
    return f"SEPA {row.get('symbol')} {row.get('path')} · grade {row.get('grade')}{tail}"


def _symbol_of(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "").upper()


def pick_opportunity(
    *,
    symbol: str,
    sepa_candidates: list[dict[str, Any]],
    mom_rows: list[dict[str, Any]],
    sepa_lamp: str,
    mom_lamp: str,
) -> tuple[str, str, str, dict[str, Any] | None]:
    """``(text, lamp, route, sepa_pick)`` for the Opportunity card.

    ``sepa_pick`` is the SEPA row the card is about, or None — the caller offers
    "View opportunity" only when there is one and its lamp is green.
    """
    sym_key = symbol.strip().upper()
    market_wide = sym_key in MARKET_WIDE_SYMBOLS
    own_rows = [r for r in sepa_candidates if _symbol_of(r) == sym_key]
    other_rows = [r for r in sepa_candidates if _symbol_of(r) != sym_key]
    own_pick = _sepa_pick(own_rows)
    market_pick = _sepa_pick(other_rows) if market_wide or not own_pick else None
    own_a_plus = next((r for r in mom_rows if _symbol_of(r) == sym_key and r.get("grade") == "A+"), None)
    market_a_plus = next((r for r in mom_rows if r.get("grade") == "A+"), None)

    if own_pick:
        return _sepa_text(own_pick), sepa_lamp, SEPA_ROUTE, own_pick
    if own_a_plus:
        text = f"Momentum {sym_key} A+ · score {float(own_a_plus.get('score') or 0):.0f}"
        return text, mom_lamp, MOMENTUM_ROUTE, None
    if market_wide and market_pick:
        return _sepa_text(market_pick), sepa_lamp, SEPA_ROUTE, market_pick
    if market_wide and market_a_plus:
        text = f"Momentum {market_a_plus.get('symbol')} A+ · score {float(market_a_plus.get('score') or 0):.0f}"
        return text, mom_lamp, MOMENTUM_ROUTE, None
    if market_pick:
        return f"No SEPA setup for {sym_key} · top today: {_sepa_text(market_pick)}", "gray", SEPA_ROUTE, None
    if market_a_plus:
        text = f"No SEPA / momentum setup for {sym_key} · top today: momentum {market_a_plus.get('symbol')} A+"
        return text, "gray", MOMENTUM_ROUTE, None
    return f"No SEPA / Momentum opportunity for {sym_key} today", "gray", SEPA_ROUTE, None


def sentiment_card_verdict(sentiment: dict[str, Any] | None, symbol: str) -> str:
    """What the sentiment card says: a tape reading, or an honest 'no tape'."""
    if sentiment is None:
        return f"No sentiment for {symbol}"
    when = sentiment.get("trade_date") or "—"
    if sentiment.get("data_source") != TAPE_SOURCE:
        return f"No tape — OI proxy only, no verdict · date {when}"
    score = sentiment.get("sentiment_score")
    if score is None:
        return f"Tape present, no score · date {when}"
    return f"Net bias {float(score):+.0f} (tape) · date {when}"


def load_sepa_symbol(conn: Any, symbol: str, trade_date: date) -> dict[str, Any] | None:
    """The symbol's own SEPA row for the day, whatever its path — the brief is about it."""
    cols = ("symbol", "trade_date", "sepa_score", "grade", "stage", "path", "computed_at")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(cols)}
            FROM features.stock_signal_sepa_daily
            WHERE symbol = %s AND trade_date = %s
            LIMIT 1
            """,
            (symbol.strip().upper(), trade_date),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    row = dict(zip(cols, raw)) if not isinstance(raw, dict) else dict(raw)
    if isinstance(row.get("trade_date"), date):
        row["trade_date"] = row["trade_date"].isoformat()
    return row
