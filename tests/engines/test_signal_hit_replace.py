"""A re-walked day is replaced, not only upserted.

Re-walking upserted what fires on today's view of a date and never deleted, so
a trigger that stopped firing kept its row beside the new ones. The opex_pin
rule change (2026-09-26) left 808 such rows and the iv_rank / vrp source
recompute (2026-09-27) about 6,800; every hit rate read both. A day is now
replaced for each lens whose source has it — and only for those, because a
loader reads nothing before the trading-day batch writes the session.
"""

from __future__ import annotations

from datetime import date

import pytest

from bifrost_research.engines.signal_hit import entry

DAY = date(2026, 9, 23)


class _Cur:
    def __init__(self, outer):
        self.outer = outer

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.outer.statements.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return self.outer.existing

    def fetchone(self):
        sql = self.outer.statements[-1][0]
        return (1,) if any(t in sql for t in self.outer.sources_with_day) else None


class _Conn:
    def __init__(self, existing=(), sources_with_day=()):
        self.existing = list(existing)
        self.sources_with_day = list(sources_with_day)
        self.statements: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1


def _row(symbol, lens, side):
    return (DAY, symbol, lens, side, 0.0, None, None, None, None, None)


def test_every_decay_lens_names_its_source():
    assert set(entry.LENS_SOURCE) == set(entry.ALL_LENSES)


def test_unfired_keys_are_what_the_day_holds_and_no_longer_fires():
    existing = [("iv_rank", "AAPL", "hot"), ("iv_rank", "NVDA", "cold"), ("vrp", "AAPL", "hot")]
    fired = [("iv_rank", "AAPL", "hot"), ("vrp", "MSFT", "cold")]
    assert entry.unfired_keys(existing, fired) == [("iv_rank", "NVDA", "cold"), ("vrp", "AAPL", "hot")]


def test_replace_deletes_only_the_unfired_rows_of_that_day():
    conn = _Conn(existing=[("iv_rank", "AAPL", "hot"), ("iv_rank", "NVDA", "cold")])
    n = entry.replace_unfired(conn, DAY, ["iv_rank"], [_row("AAPL", "iv_rank", "hot")])

    assert n == 1
    select, delete = conn.statements
    assert "lens = ANY(%s)" in select[0] and select[1] == [DAY, ["iv_rank"]]
    assert delete[0].startswith("DELETE FROM") and "trade_date = %s" in delete[0]
    assert delete[1] == (DAY, ["iv_rank"], ["NVDA"], ["cold"])
    assert conn.commits == 1


def test_a_watchlist_run_replaces_inside_the_watchlist_only():
    conn = _Conn(existing=[])
    entry.replace_unfired(conn, DAY, ["vrp"], [], symbols=["spy", "qqq"])

    select = conn.statements[0]
    assert "symbol = ANY(%s)" in select[0]
    assert select[1] == [DAY, ["vrp"], ["SPY", "QQQ"]]


def test_a_lens_without_its_source_keeps_the_day():
    conn = _Conn(existing=[("iv_rank", "AAPL", "hot")])
    assert entry.replace_unfired(conn, DAY, [], []) == 0
    assert conn.statements == []


def test_dry_run_counts_and_writes_nothing():
    conn = _Conn(existing=[("iv_rank", "AAPL", "hot"), ("iv_rank", "NVDA", "cold")])
    assert entry.replace_unfired(conn, DAY, ["iv_rank"], [], dry_run=True) == 2
    assert not any(s.startswith("DELETE") for s, _ in conn.statements)
    assert conn.commits == 0


def test_only_lenses_whose_source_has_the_day_may_replace_it():
    conn = _Conn(sources_with_day=["features.stock_signal_vrp_daily"])
    assert entry.lenses_with_source(conn, DAY, ["iv_rank", "vrp"]) == ["vrp"]
    assert all(p == (DAY,) for _, p in conn.statements)


def test_run_replaces_each_day_and_dry_run_skips_the_upsert(monkeypatch):
    conn = _Conn()
    calls: dict[str, list] = {"upsert": [], "replace": []}
    monkeypatch.setattr(entry, "connect", lambda: conn)
    monkeypatch.setattr(entry, "_trading_days", lambda c, s, e: [DAY])
    monkeypatch.setattr(entry, "_watchlist", lambda: [])
    monkeypatch.setattr(entry, "build_rows_for_day", lambda c, d, lenses, symbols=None: [_row("AAPL", "vrp", "hot")])
    monkeypatch.setattr(entry, "lenses_with_source", lambda c, d, lenses: ["vrp"])
    monkeypatch.setattr(entry, "batch_upsert", lambda *a, **k: calls["upsert"].append(a))

    def fake_replace(c, d, present, rows, *, symbols=None, dry_run=False):
        calls["replace"].append((d, present, dry_run))
        return 3

    monkeypatch.setattr(entry, "replace_unfired", fake_replace)

    out = entry.run(lookback_days=1, lenses=["iv_rank", "vrp"], as_of=DAY)
    assert len(calls["upsert"]) == 1
    assert calls["replace"] == [(DAY, ["vrp"], False)]
    assert out["rows_removed"] == 3
    assert out["days"][0]["lenses_without_source"] == ["iv_rank"]

    calls["upsert"].clear()
    dry = entry.run(lookback_days=1, lenses=["vrp"], as_of=DAY, dry_run=True)
    assert calls["upsert"] == []
    assert dry["mode"] == "dry_run" and calls["replace"][-1][2] is True


def test_the_sunday_job_rewalks_every_lens_after_the_iv_heal(monkeypatch):
    pytest.importorskip("dagster")
    from bifrost_research.orchestration import research_aux_schedules as aux
    from bifrost_research.orchestration import runners

    order: list[str] = []
    monkeypatch.setattr(runners, "run_volatility", lambda **k: order.append("volatility") or {})
    monkeypatch.setattr(runners, "run_iv_coverage_heal", lambda: order.append("heal") or {})
    monkeypatch.setattr(runners, "run_signal_hit_full_rewalk", lambda: order.append("rewalk") or {})

    out = aux._run_vol_weekly_backfill()
    assert order == ["volatility", "heal", "rewalk"]
    assert "signal_hit_rewalk" in out


def test_the_full_rewalk_spans_the_heal_and_repairs(monkeypatch):
    from bifrost_research.orchestration import runners

    seen: dict = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {"days": [{}] * 3, "rows_removed": 5}

    monkeypatch.setattr(entry, "run", fake_run)
    out = runners.run_signal_hit_full_rewalk()
    assert seen == {"lookback_days": runners.SIGNAL_HIT_FULL_REWALK_DAYS, "repair": True}
    assert runners.SIGNAL_HIT_FULL_REWALK_DAYS >= 504  # two years of sessions
    assert out["days"] == 3 and out["engine"] == "signal_hit_full_rewalk"
