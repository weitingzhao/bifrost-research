"""Manifest-bound, insert-only ATM repair. Never use the daily replacement writer.

The caller owns the transaction and supplies the reviewed exact expiry keys.
Dry runs require only reader privileges; execution requires analytics_writer.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bifrost_research.engines.adjusted_contracts import not_adjusted_contract_sql
from bifrost_research.engines.volatility.atm_iv import (
    ATM_MAX_MONEYNESS, ATM_MIN_CONTRACTS, ATM_MIN_IV,
    atm_iv_from_side_items, build_expiry_side_items, representative_spot,
)
from bifrost_research.engines.volatility.iv_solver import (
    _mid_from_ohlc, as_traded_close, degraded_sessions,
)
from bifrost_research.pricing import solve_iv
from bifrost_research.pricing.rates import load_rate_curve

LONG_MIN_DTE = 50
LONG_MAX_DTE = 220
MAX_KEYS = 756
Key = tuple[str, date, date]


def validate_keys(keys: Iterable[Key]) -> list[Key]:
    out = list(keys)
    if not out or len(out) > MAX_KEYS or len(set(out)) != len(out):
        raise ValueError("manifest must contain 1..756 unique expiry keys")
    for symbol, session, expiry in out:
        if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,15}", symbol):
            raise ValueError("invalid symbol")
        if not LONG_MIN_DTE <= (expiry - session).days <= LONG_MAX_DTE:
            raise ValueError("repair is restricted to 50..220 DTE")
    return sorted(out)


def load_manifest(path: Path, expected_sha256: str) -> list[Key]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("manifest digest differs from reviewed scope")
    rows = csv.DictReader(content.decode("utf-8").splitlines())
    return validate_keys(
        (r["symbol"], date.fromisoformat(r["session"]), date.fromisoformat(r["expiry"]))
        for r in rows
    )


def _query(conn: Any, sql: str, args: tuple[Any, ...]) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() or []


def _read_day(conn: Any, symbol: str, session: date) -> list[Any]:
    return _query(conn, """
        SELECT expiry, atm_strike, atm_iv, underlying_price, iv_source, computed_at
        FROM features.option_metric_atm_iv_daily
        WHERE symbol=%s AND trade_date=%s ORDER BY expiry
    """, (symbol, session))


def _rate(conn: Any, session: date) -> float | None:
    # No fallback: a genuine Treasury 0 survives; unknown/stale/error is not 0.
    return load_rate_curve(conn, session, session, strict=True).observed_on_or_before(session)


def source_rows(conn: Any, key: Key) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Read only the approved symbol/session/expiry; errors propagate, never clear data."""
    symbol, session, expiry = key
    reasons: Counter[str] = Counter()
    cols = ("option_ticker", "strike", "option_right", "iv", "underlying_price", "solver_status")
    stored = _query(conn, f"""
        SELECT option_ticker,strike,option_right,iv,spot,solver_status
        FROM features.option_iv_reconstructed_daily
        WHERE symbol=%s AND trade_date=%s AND expiry=%s AND iv>0 AND iv<10
        AND spot>0 AND spot<'Infinity'::float8
        AND {not_adjusted_contract_sql('option_ticker')}
    """, key)
    rows = {r[0]: dict(zip(cols, r)) for r in stored}
    degraded = session in degraded_sessions(conn, symbol, session, session)
    if degraded:
        rows = {t: r for t, r in rows.items() if r['solver_status'] != 'vendor_snapshot'}
        reasons['degraded_snapshot'] += 1
    else:
        from bifrost_research.engines.volatility.atm_iv import fetch_snapshot_iv_rows_for_date
        for r in fetch_snapshot_iv_rows_for_date(conn, session, underlyings=[symbol]):
            if r.get('expiry') != expiry:
                continue
            iv = float(r.get('iv') or 0)
            iv = iv / 100 if iv > 3 else iv
            if math.isfinite(iv) and ATM_MIN_IV <= iv <= 5:
                rows[r['option_ticker']] = {**r, 'iv': iv, 'solver_status': 'vendor_snapshot'}
    raw = _query(conn, f"""
        SELECT o.option_ticker,o.strike,o.option_right,o.close,o.high,o.low,
               {as_traded_close('s')} AS spot
        FROM raw_market.option_daily o JOIN raw_market.stock_daily s
          ON s.symbol=o.underlying AND s.bar_date=o.bar_date
        WHERE o.underlying=%s AND o.bar_date=%s AND o.expiry=%s
          AND s.symbol=%s AND s.bar_date=%s AND s.close>0
          AND {not_adjusted_contract_sql('o.option_ticker')}
    """, (*key, symbol, session))
    missing = [r for r in raw if r[0] not in rows]
    rate = _rate(conn, session) if missing else None
    for ticker, strike, right, close, high, low, spot in missing:
        if rate is None:
            reasons['missing_rate'] += 1
            continue
        strike, spot = float(strike), float(spot)
        if not math.isfinite(spot) or spot <= 0 or not 0.9 * spot <= strike <= 1.1 * spot:
            continue
        mid = _mid_from_ohlc(close, high, low)
        if mid is None:
            reasons['missing_price'] += 1
            continue
        iv, status = solve_iv(spot, strike, (expiry-session).days/365, mid, right, rate=rate)
        if iv is None:
            reasons[status] += 1
            continue
        rows[ticker] = dict(zip(cols, (ticker, strike, right, iv, spot, 'ok')))
    return list(rows.values()), reasons


def candidate(rows: list[dict[str, Any]]) -> tuple[float, float, float, str] | None:
    spot = representative_spot(rows)
    if spot is None or not math.isfinite(spot):
        return None
    # Distinct contracts, not duplicate versions of a snapshot.
    rows = list({r['option_ticker']: r for r in rows}.values())
    rows = [r for r in rows if build_expiry_side_items(
        [r], spot, max_moneyness=ATM_MAX_MONEYNESS, min_iv=ATM_MIN_IV
    )]
    items = build_expiry_side_items(rows, spot, max_moneyness=ATM_MAX_MONEYNESS, min_iv=ATM_MIN_IV)
    if len(items) < ATM_MIN_CONTRACTS:
        return None
    vendor = any(r.get('solver_status') == 'vendor_snapshot' for r in rows)
    if not vendor and not (any(i[1] is not None for i in items) and any(i[2] is not None for i in items)):
        return None
    iv, _, _, strike = atm_iv_from_side_items(items)
    return (strike, iv, spot, 'snapshot' if vendor else 'reconstructed') if iv is not None else None


def _persist_source(conn: Any, key: Key, rows: list[dict[str, Any]], expected, *, execute: bool = True) -> int:
    """Persist only the ATM witnesses inside an approved expiry (at most five).

    The currently deployed normal writer reads these beyond 90 DTE. Without
    witnesses, its Sunday replay would erase bar-derived long ATM repairs before
    the unpublished preservation guard reaches production. Existing source keys
    remain untouched; an incompatible replay aborts the entire transaction.
    """
    spot = expected[2]
    clean = sorted((r for r in rows if build_expiry_side_items(
        [r],spot,max_moneyness=ATM_MAX_MONEYNESS,min_iv=ATM_MIN_IV
    )), key=lambda r: (abs(float(r['strike'])-spot), r['option_ticker']))
    chosen = list(clean[:2])
    for right in ('C','P'):
        match = next((r for r in clean if str(r['option_right']).upper() in
                      (right, 'CALL' if right == 'C' else 'PUT')), None)
        if match is not None:
            chosen.append(match)
    vendor = next((r for r in clean if r.get('solver_status') == 'vendor_snapshot'),None)
    if vendor is not None:
        chosen.append(vendor)
    chosen = list({r['option_ticker']: r for r in chosen}.values())
    written = 0
    if execute:
        with conn.cursor() as cur:
            for r in chosen:
                cur.execute('''INSERT INTO features.option_iv_reconstructed_daily
                (symbol,option_ticker,trade_date,strike,expiry,option_right,spot,tte_years,iv,solver_status,computed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol,option_ticker,trade_date) DO NOTHING''',
                (key[0],r['option_ticker'],key[1],r['strike'],key[2],r['option_right'],
                 r['underlying_price'],(key[2]-key[1]).days/365,r['iv'],
                 r.get('solver_status','ok'),datetime.now(timezone.utc)))
                written += cur.rowcount
    stored = _query(conn, f'''SELECT option_ticker,strike,option_right,iv,spot,solver_status
        FROM features.option_iv_reconstructed_daily WHERE symbol=%s AND trade_date=%s AND expiry=%s
        AND {not_adjusted_contract_sql('option_ticker')}''', key)
    columns = ('option_ticker','strike','option_right','iv','underlying_price','solver_status')
    held = {r[0]: dict(zip(columns,r)) for r in stored}
    if not execute:
        for r in chosen:
            if r['option_ticker'] not in held:
                held[r['option_ticker']] = r
                written += 1
    # Match the deployed reconstructed reader's positive-IV / positive-spot
    # predicate, while retaining all keys above for insert-conflict simulation.
    replay = candidate([r for r in held.values() if r.get('iv') is not None
                        and float(r['iv']) > 0 and r.get('underlying_price') is not None
                        and float(r['underlying_price']) > 0])
    if replay is None or any(abs(a-b)>1e-8 for a,b in zip(replay[:3],expected[:3])):
        raise RuntimeError('stored source cannot reproduce ATM; transaction rolled back')
    return written


def repair(conn: Any, keys: Iterable[Key], *, execute: bool = False) -> dict[str, Any]:
    """One transaction; append only, preserve every existing day row byte for byte.

    A dedicated non-autocommit connection is required for execution. Advisory
    lock covers insert/readback only, so slow source reads cannot stall EOD.
    Concurrent replacement of an existing row aborts. Caller owns the commit.
    """
    keys = validate_keys(keys)
    if execute:
        if conn.autocommit:
            raise ValueError('execution needs an explicit transaction')
        identity = _query(conn, 'SELECT current_user, current_database()', ())
        if identity != [('analytics_writer', 'bifrost_golden_source')]:
            raise ValueError('execution requires the Research domain writer')
    before = {(s, d): _read_day(conn, s, d) for s, d in sorted({k[:2] for k in keys})}
    reasons: Counter[str] = Counter()
    proposed = []
    witnesses = {}
    inserted = 0
    source_inserted = 0
    source_proposed = 0
    try:
        for key in keys:
            if key[2] in {r[0] for r in before[key[:2]]}:
                reasons['existing_preserved'] += 1
                continue
            rows, why = source_rows(conn, key)
            reasons.update(why)
            value = candidate(rows)
            if value is None:
                reasons['insufficient_valid_contracts'] += 1
                continue
            if not execute:
                source_proposed += _persist_source(conn,key,rows,value,execute=False)
            proposed.append((*key, *value))
            witnesses[key] = rows
        if execute:
            with conn.cursor() as cur:
                cur.execute('SET LOCAL lock_timeout = \'5s\'')
                cur.execute('LOCK TABLE features.option_metric_atm_iv_daily IN SHARE ROW EXCLUSIVE MODE')
            current = {(s,d): _read_day(conn,s,d) for s,d in before}
            for sd, old in before.items():
                now_by_expiry = {r[0]: r for r in current[sd]}
                if any(now_by_expiry.get(r[0]) != r for r in old):
                    raise RuntimeError('existing rows changed concurrently; repeat dry run')
            before = current
            proposed = [row for row in proposed if row[2] not in {r[0] for r in before[row[:2]]}]
            for row in proposed:
                source_inserted += _persist_source(conn,row[:3],witnesses[row[:3]],row[3:])
            with conn.cursor() as cur:
                for row in proposed:
                    cur.execute("""
                        INSERT INTO features.option_metric_atm_iv_daily
                        (symbol,trade_date,expiry,atm_strike,atm_iv,underlying_price,iv_source,computed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol,trade_date,expiry) DO NOTHING
                    """, (*row, datetime.now(timezone.utc)))
                    inserted += cur.rowcount
            for (s, d), old in before.items():
                after = {r[0]: r for r in _read_day(conn, s, d)}
                if any(after.get(r[0]) != r for r in old):
                    raise RuntimeError('preservation check failed; transaction rolled back')
            for row in proposed:
                held = {r[0]: r for r in _read_day(conn, row[0], row[1])}
                if held.get(row[2], (None,))[:5] != (row[2], *row[3:]):
                    raise RuntimeError('insert readback failed; transaction rolled back')
    except Exception:
        if execute:
            conn.rollback()
        raise
    return {'keys': len(keys), 'proposed': len(proposed), 'inserted': inserted,
            'source_inserted': source_inserted,
            'source_proposed': source_proposed,
            'existing_rows_preserved': sum(len(v) for v in before.values()),
            'reasons': dict(reasons), 'execute': execute, 'rows': proposed}
