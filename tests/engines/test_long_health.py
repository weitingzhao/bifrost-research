from datetime import date, timedelta

from bifrost_research.engines.volatility.long_health import judge


def test_bracket_breadth_does_not_count_one_sided_rows_or_remove_missing_session():
    days = [date(2026,8,10)+timedelta(days=i) for i in range(4)]
    rows = [(s,d,d+timedelta(days=tenor),.3) for s in ['SPY','NVDA'] for d in days[:3] for tenor in [40,100,200]]
    rows.append(('SPY',days[3],days[3]+timedelta(days=200),.3))
    out = judge(rows, days, ['SPY','NVDA'])
    assert all(r['valid_symbols'] == 0 and r['status'] == 'missing_data' for r in out)
    assert all(r['baseline'] == 2 for r in out)


def test_missing_and_insufficient_history_are_independent():
    result = judge([], [date(2026, 8, 10)], ['SPY', 'SPY'])
    assert all(r['coverage'] == 0 and r['expected_symbols'] == 1 for r in result)
    assert all(r['data_status'] == 'missing_data' and
               r['baseline_status'] == 'history_insufficient' for r in result)


def test_eroding_relative_baseline_does_not_hide_absolute_coverage():
    days = [date(2026, 8, 10) + timedelta(days=i) for i in range(5)]
    rows = [('SPY', d, d+timedelta(days=t), .3) for d in days for t in [40,100,200]]
    result = judge(rows, days, ['SPY', 'NVDA'])
    assert all(r['status'] == 'ok' and r['coverage'] == .5 for r in result)


def test_missing_calendar_session_cannot_borrow_yesterdays_coverage():
    from bifrost_research.engines.volatility.long_health import read_health
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None): pass
        def fetchall(self): return [(date(2026, 8, 10),)]
    class Conn:
        def cursor(self): return Cursor()
    result = read_health(Conn(), ['SPY'], date(2026, 8, 11))
    assert result[0]['status'] == 'unprobed'
