from datetime import date, timedelta

import pytest

from bifrost_research.engines.volatility import atm_repair as r

D = date(2026, 8, 10)
KEY = ('SPY', D, D+timedelta(days=100))


def row(ticker, right='C', iv=.3, **kw):
    return dict(option_ticker=ticker, option_right=right, strike=100., iv=iv,
                underlying_price=100., solver_status='ok', **kw)


def test_long_manifest_is_bounded_and_digest_verified(tmp_path):
    from bifrost_research.repositories.iv_cone import TENORS
    assert r.LONG_MIN_DTE <= 60 and r.LONG_MAX_DTE >= max(TENORS)
    assert r.validate_keys([KEY]) == [KEY]
    for keys in ([KEY, KEY], [], [('SPY', D, D+timedelta(days=221))]):
        with pytest.raises(ValueError):
            r.validate_keys(keys)
    p = tmp_path/'keys.csv'
    p.write_text('symbol,session,expiry\nSPY,2026-08-10,2026-11-18\n')
    with pytest.raises(ValueError, match='digest'):
        r.load_manifest(p, '0'*64)


def test_brent_two_puts_and_duplicate_versions_are_not_a_valid_pair():
    assert r.candidate([row('p1', 'P'), row('p2', 'P')]) is None
    assert r.candidate([row('c1'), row('c1')]) is None
    assert r.candidate([row('c1'), row('p1', 'P')]) == (100., .3, 100., 'reconstructed')


def test_invalid_vendor_cannot_exempt_brent_pair_gate():
    vendor = row('v', iv=0)
    vendor['solver_status'] = 'vendor_snapshot'
    assert r.candidate([row('p1', 'P'), row('p2', 'P'), vendor]) is None


def test_real_zero_rate_and_missing_rate_are_distinct(monkeypatch):
    from bifrost_research.pricing.rates import RateCurve
    monkeypatch.setattr(r, 'load_rate_curve', lambda *args, **kw: RateCurve({D: 0.}))
    assert r._rate(None, D) == 0
    monkeypatch.setattr(r, 'load_rate_curve', lambda *args, **kw: RateCurve({}))
    assert r._rate(None, D) is None


class Conn:
    autocommit = False
    rolled_back = False
    statements = []
    rowcount = 1
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def execute(self, sql, args=()):
        self.statements.append((sql, args))
    def fetchall(self):
        return [('analytics_writer','bifrost_golden_source')]
    def rollback(self):
        self.rolled_back = True


def test_existing_zero_or_invalid_row_is_preserved_without_any_source_read(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(r, '_read_day', lambda *a: [(KEY[2], 100, 0, 100, 'snapshot', None)])
    monkeypatch.setattr(r, 'source_rows', lambda *a: pytest.fail('existing key must not be recomputed'))
    result = r.repair(conn, [KEY], execute=True)
    assert result['inserted'] == 0 and result['reasons']['existing_preserved'] == 1
    assert not any('DELETE' in sql or 'UPDATE' in sql for sql, _ in conn.statements)


def test_readback_failure_rolls_back_whole_batch(monkeypatch):
    conn = Conn()
    reads = iter([[], [], [], []])
    monkeypatch.setattr(r, '_read_day', lambda *a: next(reads))
    monkeypatch.setattr(r, 'source_rows', lambda *a: ([row('c'), row('p', 'P')], {}))
    monkeypatch.setattr(r, '_persist_source', lambda *a: 0)
    with pytest.raises(RuntimeError, match='readback'):
        r.repair(conn, [KEY], execute=True)
    assert conn.rolled_back


def test_long_insert_keeps_short_expiry_and_second_run_is_idempotent(monkeypatch):
    short = (D+timedelta(days=20),100.,.3,100.,'snapshot',None)
    held = [short]
    class Writer(Conn):
        def execute(self, sql, args=()):
            super().execute(sql,args)
            if 'INSERT INTO' in sql:
                held.append((args[2], *args[3:]))
    conn = Writer()
    monkeypatch.setattr(r, '_read_day', lambda *a: list(held))
    calls = []
    def source(*args):
        calls.append(args)
        return [row('c'),row('p','P')], {}
    monkeypatch.setattr(r, 'source_rows', source)
    monkeypatch.setattr(r, '_persist_source', lambda *a: 2)
    assert r.repair(conn,[KEY],execute=True)['inserted'] == 1
    assert held[0] == short
    assert r.repair(conn,[KEY],execute=True)['inserted'] == 0
    assert len(calls) == 1 and len(held) == 2


def test_persist_witnesses_is_bounded_insert_only_and_replay_checked(monkeypatch):
    conn = Conn()
    rows = [row('O:SPYC'),row('O:SPYP','P')]
    stored = [(v['option_ticker'],v['strike'],v['option_right'],v['iv'],v['underlying_price'],v['solver_status']) for v in rows]
    monkeypatch.setattr(r,'_query',lambda *a: stored)
    assert r._persist_source(conn,KEY,rows,(100.,.3,100.,'reconstructed')) == 2
    inserts = [sql for sql, _ in conn.statements if 'INSERT INTO features.option_iv_reconstructed_daily' in sql]
    assert all('DO NOTHING' in sql and 'UPDATE' not in sql for sql in inserts)
    monkeypatch.setattr(r,'_query',lambda *a: [])
    with pytest.raises(RuntimeError,match='reproduce ATM'):
        r._persist_source(conn,KEY,rows,(100.,.3,100.,'reconstructed'))
