from datetime import date, timedelta

from bifrost_research.engines.volatility.long_health import judge


def test_bracket_breadth_does_not_count_one_sided_rows_or_remove_missing_session():
    days = [date(2026,8,10)+timedelta(days=i) for i in range(4)]
    rows = [(s,d,d+timedelta(days=tenor),.3) for s in ['SPY','NVDA'] for d in days[:3] for tenor in [40,100,200]]
    rows.append(('SPY',days[3],days[3]+timedelta(days=200),.3))
    out = judge(rows, days, ['SPY','NVDA'])
    assert all(r['valid_symbols'] == 0 and r['status'] == 'missing_data' for r in out)
    assert all(r['baseline'] == 2 for r in out)
