"""Valid tenor breadth, independent of producer row counts (TD-172/142)."""
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any, Sequence

from bifrost_research.repositories.iv_cone import tenor_iv


def judge(rows, sessions, symbols):
    by_day = defaultdict(list)
    for symbol, day, expiry, iv in rows:
        by_day[symbol, day].append((expiry, iv))
    out = []
    for tenor in (60, 90, 180):
        counts = [sum(tenor_iv(d, by_day[s, d], tenor) is not None for s in symbols) for d in sessions]
        baseline = median(counts[-11:-1]) if len(counts) > 1 else None
        n = counts[-1] if counts else 0
        status = ('missing_data' if n == 0 else 'history_insufficient'
                  if len(counts) < 4 or baseline == 0 else 'narrow'
                  if n < .8 * baseline else 'ok')
        out.append({'tenor': tenor, 'valid_symbols': n, 'baseline': baseline, 'status': status})
    return out


def read_health(conn: Any, symbols: Sequence[str], session: date):
    start = session - timedelta(days=45)
    try:
        with conn.cursor() as cur:
            cur.execute('SET LOCAL statement_timeout = \'20s\'')
            cur.execute('''SELECT trade_date FROM dw_stock.dim_trading_calendar
                WHERE trade_date BETWEEN %s AND %s AND is_trading_day ORDER BY trade_date''', (start, session))
            days = [r[0] for r in cur.fetchall()]
            rows = []
            syms = sorted(set(symbols))
            for offset in range(0, len(syms), 64):
                cur.execute('''SELECT symbol,trade_date,expiry,atm_iv FROM features.option_metric_atm_iv_daily
                    WHERE trade_date BETWEEN %s AND %s AND symbol=ANY(%s) AND atm_iv>0 AND atm_iv<10''',
                    (start, session, syms[offset:offset+64]))
                rows.extend(cur.fetchall())
        return judge(rows, days, syms)
    except Exception:
        conn.rollback()
        return [{'status': 'unprobed', 'reason': 'valid bracket query failed'}]
