"""Which option contracts must keep their history — ``research.option_pinned_contract``.

The Plugin's option retention is a rolling window, and that window is blind to
the one thing that must never age out: the contracts the Owner actually traded.
A post-mortem on a spread closed in March needs those legs' bars in September,
and a held leg needs its whole life, not the last ninety sessions. This engine
writes the list; the Plugin reads it to backfill (P7) and to keep those tickers
out of its delete candidates (P8).

Two reasons to pin:

- ``held`` — an option leg in the current position attribution.
- ``closed_recent`` — an option that traded inside the last 180 days and is not
  held now.

Everything comes from the Trade API's read-only HTTP surface (positions and
executions); this engine never reads the Trade database and never reads
``raw_broker``. If the Trade API cannot be reached the whole round is skipped —
an empty answer from a down dependency would read as "nothing is pinned", which
is the one wrong answer here.

IB identifies a leg as ``SYM|OPT|YYYYMMDD|STRIKE|R``; the Plugin's catalog keys
contracts by Polygon's ``option_ticker``. The two are joined through
``raw_market.option_contract`` on underlying / expiry / strike / right — no
ticker string is ever assembled here, so adjusted contracts (``O:BDX1…``) are
found if the catalog has them and reported as unmatched if it does not.

Writes ``research.option_pinned_contract`` only, and only by upsert: a row past
its ``pin_until`` stops being read, it is not deleted. D10 BLOCKED.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from bifrost_research.db.conn import connect
from bifrost_research.schema.schemas import TABLE_RESEARCH_OPTION_PINNED_CONTRACT

logger = logging.getLogger(__name__)

OPTION_CONTRACT = "raw_market.option_contract"
#: An option that traded inside this window is still worth a post-mortem.
CLOSED_RECENT_DAYS = 180
#: How long after the last sighting a contract stays pinned.
PIN_TAIL_DAYS = 180
#: ``first_pinned`` is the earliest trade in the contract, which can predate the
#: recent window, so the execution read goes back further than the pin rule.
EXECUTIONS_LOOKBACK_DAYS = 3 * 365
EXECUTIONS_LIMIT = 10000
REASON_HELD = "held"
REASON_CLOSED = "closed_recent"


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ── the IB key ────────────────────────────────────────────────────────────


def underlying_of(raw: Any) -> str | None:
    """Root from an IB symbol field, which for an option is the OCC local symbol.

    ``"NVDA  261120C00245000"`` and ``"NVDA"`` both name NVDA; the OCC string
    pads the root to six characters, so the first whitespace-separated token is
    the root.
    """
    token = str(raw or "").strip().split()
    return token[0].upper() if token else None


def canonical_root(underlying: str) -> str | None:
    """``BDX1`` → ``BDX`` — the OCC adjusted-root convention, for a second catalog probe."""
    stripped = underlying.rstrip("0123456789")
    return stripped if stripped and stripped != underlying else None


def parse_contract_key(key: Any) -> dict[str, Any] | None:
    """``SYM|OPT|YYYYMMDD|STRIKE|R`` → ``{underlying, expiry, strike, option_right}``.

    Anything that is not a complete option key — a stock leg, a truncated key, a
    strike that is not a number — returns None rather than a guess.
    """
    parts = [p.strip() for p in str(key or "").split("|")]
    if len(parts) < 5 or parts[1].upper() != "OPT":
        return None
    underlying = underlying_of(parts[0])
    right = parts[4].upper()
    if not underlying or right not in ("C", "P"):
        return None
    try:
        expiry = datetime.strptime(parts[2], "%Y%m%d").date()
        strike = float(parts[3])
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None
    return {"underlying": underlying, "expiry": expiry, "strike": strike, "option_right": right}


def pin_until(expiry: date, last_seen: date) -> date:
    """A contract stays pinned until it expires, and for 180 days after its last sighting."""
    return max(expiry, last_seen + timedelta(days=PIN_TAIL_DAYS))


def _match_key(leg: Mapping[str, Any]) -> tuple[str, date, float, str]:
    return (
        str(leg["underlying"]).upper(),
        leg["expiry"],
        round(float(leg["strike"]), 4),
        str(leg["option_right"]).upper(),
    )


# ── inputs (Trade API, read-only HTTP) ────────────────────────────────────


def load_held_legs(get: Any, base: str) -> list[dict[str, Any]]:
    """Option legs in the current position attribution."""
    payload = get(base, "/executions/position-attribution", {"sec_type": "OPT"})
    rows = (payload or {}).get("attributions") or []
    legs: list[dict[str, Any]] = []
    for row in rows:
        leg = parse_contract_key(row.get("contract_key"))
        if leg is not None:
            legs.append(leg)
    return legs


def load_option_executions(get: Any, base: str, *, since: date) -> list[dict[str, Any]]:
    """Option executions since ``since``, each with the date it happened."""
    since_ts = datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp()
    payload = get(base, "/executions", {"since_ts": since_ts, "limit": EXECUTIONS_LIMIT})
    rows = (payload or {}).get("executions") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("sec_type") or "").upper() != "OPT":
            continue
        leg = parse_contract_key(row.get("contract_key"))
        if leg is None:
            continue
        traded = _execution_date(row)
        if traded is None:
            continue
        out.append({**leg, "traded_on": traded})
    return out


def _execution_date(row: Mapping[str, Any]) -> date | None:
    raw = row.get("trade_date")
    if raw:
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    ts = row.get("time")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


# ── the rule ──────────────────────────────────────────────────────────────


def build_pins(
    *,
    as_of: date,
    held: Iterable[Mapping[str, Any]],
    executions: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, date, float, str], dict[str, Any]]:
    """Pure: one pin per contract, ``held`` winning over ``closed_recent``."""
    pins: dict[tuple[str, date, float, str], dict[str, Any]] = {}
    first_trade: dict[tuple[str, date, float, str], date] = {}
    last_trade: dict[tuple[str, date, float, str], date] = {}
    for row in executions:
        key = _match_key(row)
        traded = row["traded_on"]
        if key not in first_trade or traded < first_trade[key]:
            first_trade[key] = traded
        if key not in last_trade or traded > last_trade[key]:
            last_trade[key] = traded

    def place(key: tuple[str, date, float, str], leg: Mapping[str, Any], reason: str, last_seen: date) -> None:
        # A leg with no execution on record is pinned from the day it was seen;
        # inventing an earlier date would tell the Plugin to backfill history the
        # Owner never traded.
        first = first_trade.get(key, last_seen)
        pins[key] = {
            "underlying": leg["underlying"],
            "expiry": leg["expiry"],
            "strike": float(leg["strike"]),
            "option_right": leg["option_right"],
            "reason": reason,
            "first_pinned": min(first, last_seen),
            "last_seen": last_seen,
            "pin_until": pin_until(leg["expiry"], last_seen),
        }

    recent_cutoff = as_of - timedelta(days=CLOSED_RECENT_DAYS)
    for leg in held:
        place(_match_key(leg), leg, REASON_HELD, as_of)
    for row in executions:
        key = _match_key(row)
        if key in pins:
            continue
        seen = last_trade[key]
        if seen >= recent_cutoff:
            place(key, row, REASON_CLOSED, seen)
    return pins


# ── the catalog join ──────────────────────────────────────────────────────


def load_catalog(
    conn: Any, keys: Iterable[tuple[str, date, float, str]]
) -> dict[tuple[str, date, float, str], list[str]]:
    """``(underlying, expiry, strike, right) → [option_ticker]`` from the Plugin's catalog.

    Adjusted roots are probed as well: an IB leg on ``BDX1`` is the same contract
    the catalog files under ``BDX`` with an ``O:BDX1…`` ticker.
    """
    keys = list(keys)
    if not keys:
        return {}
    roots: set[str] = set()
    for underlying, _expiry, _strike, _right in keys:
        roots.add(underlying)
        root = canonical_root(underlying)
        if root:
            roots.add(root)
    expiries = sorted({k[1] for k in keys})
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT option_ticker, underlying, expiry, strike, option_right
            FROM {OPTION_CONTRACT}
            WHERE underlying = ANY(%s) AND expiry = ANY(%s)
            """,
            (sorted(roots), expiries),
        )
        rows = cur.fetchall() or []
    catalog: dict[tuple[str, date, float, str], list[str]] = {}
    for ticker, underlying, expiry, strike, right in rows:
        key = (
            str(underlying).upper(),
            expiry,
            round(float(strike), 4),
            str(right).upper(),
        )
        catalog.setdefault(key, []).append(str(ticker))
    return catalog


def resolve_tickers(
    pins: Mapping[tuple[str, date, float, str], Mapping[str, Any]],
    catalog: Mapping[tuple[str, date, float, str], list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(rows to write, unmatched)`` — every ticker the catalog offers for a pinned leg.

    When a leg matches more than one ticker (an adjusted family), all of them are
    pinned: the IB key cannot tell them apart, and pinning one contract too many
    costs a few backfilled bars while dropping one loses history for good.
    """
    rows: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for key, pin in sorted(pins.items()):
        underlying, expiry, strike, right = key
        tickers = catalog.get(key)
        if not tickers:
            root = canonical_root(underlying)
            tickers = catalog.get((root, expiry, strike, right)) if root else None
        if not tickers:
            unmatched.append(
                {
                    "underlying": underlying,
                    "expiry": expiry.isoformat(),
                    "strike": strike,
                    "option_right": right,
                    "reason": pin["reason"],
                    "why": "no matching contract in raw_market.option_contract",
                }
            )
            continue
        for ticker in sorted(tickers):
            rows.append({**pin, "option_ticker": ticker, "ambiguous": len(tickers) > 1})
    return rows, unmatched


# ── the write ─────────────────────────────────────────────────────────────


def write_pins(conn: Any, rows: list[dict[str, Any]]) -> int:
    """Upsert only. ``first_pinned`` never moves forward and the pin never shortens."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {TABLE_RESEARCH_OPTION_PINNED_CONTRACT}
                (option_ticker, underlying, expiry, strike, option_right,
                 reason, first_pinned, last_seen, pin_until, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (option_ticker) DO UPDATE SET
                reason = EXCLUDED.reason,
                first_pinned = LEAST({TABLE_RESEARCH_OPTION_PINNED_CONTRACT}.first_pinned,
                                     EXCLUDED.first_pinned),
                last_seen = GREATEST({TABLE_RESEARCH_OPTION_PINNED_CONTRACT}.last_seen,
                                     EXCLUDED.last_seen),
                pin_until = GREATEST({TABLE_RESEARCH_OPTION_PINNED_CONTRACT}.pin_until,
                                     EXCLUDED.pin_until),
                updated_at = now()
            """,
            [
                (
                    r["option_ticker"],
                    r["underlying"],
                    r["expiry"],
                    r["strike"],
                    r["option_right"],
                    r["reason"],
                    r["first_pinned"],
                    r["last_seen"],
                    r["pin_until"],
                )
                for r in rows
            ],
        )
    conn.commit()
    return len(rows)


def run(*, as_of: date | None = None) -> dict[str, Any]:
    from bifrost_research.mcp.tools._trade_api_client import base_trading, get

    day = as_of or _today()
    try:
        base = base_trading()
        held = load_held_legs(get, base)
        executions = load_option_executions(
            get, base, since=day - timedelta(days=EXECUTIONS_LOOKBACK_DAYS)
        )
    except Exception as exc:  # noqa: BLE001 — a down Trade API must not empty the list
        logger.warning("option_pinned_contract skipped: trade api unavailable: %s", exc)
        return {
            "engine": "option_pinned_contract",
            "as_of": day.isoformat(),
            "mode": "skipped",
            "reason": f"trade api unavailable: {exc}",
            "rows_written": 0,
        }

    pins = build_pins(as_of=day, held=held, executions=executions)
    conn = connect()
    try:
        catalog = load_catalog(conn, pins.keys())
        rows, unmatched = resolve_tickers(pins, catalog)
        written = write_pins(conn, rows)
    finally:
        conn.close()
    return {
        "engine": "option_pinned_contract",
        "as_of": day.isoformat(),
        "mode": "written",
        "held_legs": sum(1 for p in pins.values() if p["reason"] == REASON_HELD),
        "closed_recent_legs": sum(1 for p in pins.values() if p["reason"] == REASON_CLOSED),
        "rows_written": written,
        "ambiguous": sum(1 for r in rows if r["ambiguous"]),
        "unmatched_count": len(unmatched),
        "unmatched": unmatched[:50],
        "executions_seen": len(executions),
    }
