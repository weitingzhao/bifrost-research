"""K6 distiller — pairing arithmetic and the §20 store rules.

Fixtures are invented (never copied from the live book); the SQL beneath is
exercised against the real store on DEV.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bifrost_research.engines.journal_distill import (
    ARCHIVE_STRENGTH,
    candidates_from_decisions,
    candidates_from_pairs,
    candidates_from_visits,
    pair_option_fills,
    resolve_change,
)

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def _fill(key: str, sym: str, side: str, qty: float, price: float, at: datetime) -> dict:
    return {
        "contract_key": key,
        "symbol": sym,
        "sec_type": "OPT",
        "side": side,
        "quantity": qty,
        "price": price,
        "exec_time": at,
    }


def test_pair_closes_when_net_returns_to_flat() -> None:
    pairs = pair_option_fills(
        [
            _fill("ZZTM-P100", "ZZTM", "SELL", 2, 1.20, T0),
            _fill("ZZTM-P100", "ZZTM", "BUY", 2, 0.50, T0 + timedelta(days=9)),
        ]
    )
    assert len(pairs) == 1
    p = pairs[0]
    assert p["symbol"] == "ZZTM" and p["days"] == 9 and p["short_open"] is True
    # sell 2 × 1.20 × 100 − buy 2 × 0.50 × 100
    assert round(p["realized"]) == 140
    assert round(p["open_cash"]) == 240


def test_pair_skips_stock_rows_and_open_positions() -> None:
    rows = [
        _fill("ZZTM-P100", "ZZTM", "SELL", 1, 1.0, T0),  # never closed
        {**_fill("k", "ZZTM", "BUY", 10, 50.0, T0), "sec_type": "STK"},
    ]
    assert pair_option_fills(rows) == []


def _short_pair(sym: str, i: int, kept_frac: float, loss: bool = False) -> list[dict]:
    open_t = T0 + timedelta(days=3 * i)
    close_t = open_t + timedelta(days=5)
    open_px = 2.0
    close_px = open_px * (1 - kept_frac) if not loss else open_px * 1.5
    key = f"{sym}-P{i}"
    return [
        _fill(key, sym, "SELL", 1, open_px, open_t),
        _fill(key, sym, "BUY", 1, close_px, close_t),
    ]


def test_candidates_measure_hold_exit_and_the_weak_spot() -> None:
    rows: list[dict] = []
    for i in range(6):
        rows += _short_pair("ZZTM", i, kept_frac=0.6)
    for i in range(3):
        rows += _short_pair("QQXX", 100 + i, kept_frac=0.0, loss=True)
    cands = {c.topic: c for c in candidates_from_pairs(pair_option_fills(rows))}
    assert cands["axis-hold"].value == "5d"
    assert cands["axis-exit"].value == "Half the credit"
    trig = cands["axis-trigger"]
    assert trig.kind == "tension" and trig.value == "QQXX" and trig.symbols == ("QQXX",)


def test_candidates_stay_absent_below_the_floor() -> None:
    rows = _short_pair("ZZTM", 0, kept_frac=0.6) + _short_pair("ZZTM", 1, kept_frac=0.6)
    assert candidates_from_pairs(pair_option_fills(rows)) == []


def test_decisions_candidate_reads_draft_counts() -> None:
    today = date(2026, 1, 9)
    assert candidates_from_decisions({"pending": 3, "approved": 1}, today=today) == []
    (c,) = candidates_from_decisions(
        {"pending": 10, "approved": 2, "dismissed": 6}, today=today
    )
    assert "8 cards decided" in c.text_md and "10 still pending" in c.text_md


def test_visits_candidate_needs_five_reads_of_a_name() -> None:
    today = date(2026, 1, 9)
    rows = [{"symbol": "ZZTM"}] * 4
    assert candidates_from_visits(rows, today=today) == []
    (c,) = candidates_from_visits(rows + [{"symbol": "ZZTM"}], today=today)
    assert "ZZTM ×5" in c.text_md and c.symbols == ("ZZTM",)


def test_resolve_change_and_the_archive_floor() -> None:
    today = date(2026, 1, 9)
    assert resolve_change(None, 0.5, today=today) == ("new", False)
    prev = {"strength": 0.5, "last_seen": date(2026, 1, 8), "change": "stronger"}
    assert resolve_change(prev, 0.62, today=today)[0] == "stronger"
    assert resolve_change(prev, 0.30, today=today)[0] == "fading"
    assert resolve_change(prev, 0.52, today=today)[0] == "steady"
    # §20.3 — fading's end is the archive, never deletion
    low = resolve_change(prev, ARCHIVE_STRENGTH / 2, today=today)
    assert low == ("fading", True)
    # A same-day re-run keeps the morning's verdict
    same_day = {"strength": 0.5, "last_seen": today, "change": "stronger"}
    assert resolve_change(same_day, 0.51, today=today)[0] == "stronger"


def test_owner_universe_includes_the_auth_registry(monkeypatch) -> None:
    """A trader who has never written a note still gets fills-based memories."""
    from bifrost_research.auth.bearer import known_owner_ids, token_to_owner_map

    token_to_owner_map.cache_clear()
    monkeypatch.setenv("RESEARCH_USERS", "alice:tok_a,bob:tok_b")
    assert known_owner_ids() == ("alice", "bob")
    token_to_owner_map.cache_clear()
    monkeypatch.delenv("RESEARCH_USERS", raising=False)
    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    assert known_owner_ids() == ("owner",)
    token_to_owner_map.cache_clear()


def test_earnings_weak_spot_needs_samples_and_a_net_loss() -> None:
    from bifrost_research.engines.journal_distill import candidates_from_earnings

    def pairs(n: int, realized: float) -> list[dict]:
        out = []
        for i in range(n):
            opened = T0 + timedelta(days=30 * i)
            out.append(
                {
                    "symbol": "ZZTM",
                    "opened": opened,
                    "closed": opened + timedelta(days=5),
                    "realized": realized,
                }
            )
        return out
    prints = {"ZZTM": [(T0 + timedelta(days=30 * i, hours=30)).date() for i in range(6)]}
    # two samples — absent
    assert candidates_from_earnings(pairs(2, -100), prints) == []
    # three samples but net positive — a profitable habit is not a warning
    assert candidates_from_earnings(pairs(3, 100), prints) == []
    (c,) = candidates_from_earnings(pairs(3, -100), prints)
    assert c.topic == "axis-trigger-earnings" and c.value == "Earnings week"
    assert "3 closed" in c.text_md and c.symbols == ("ZZTM",)
    # opens far from any print — absent
    far = {"ZZTM": [(T0 - timedelta(days=300)).date()]}
    assert candidates_from_earnings(pairs(3, -100), far) == []


# ── the Flex gate (TD-244): no fills memories from a stale book ──────────────


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rowcount = 0
        self._rows: list[dict] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.conn.sql.append(sql)
        if "raw_broker.executions_final" in sql:
            self._rows = list(self.conn.fills)
        elif "SELECT DISTINCT owner_id" in sql:
            self._rows = [{"owner_id": "alice"}]
        elif "INSERT INTO" in sql:
            self.conn.topics.append(params[1])  # type: ignore[index]
            self._rows = [{"mem_no": len(self.conn.topics)}]
        else:
            self._rows = []

    def fetchall(self) -> list[dict]:
        return self._rows

    def fetchone(self) -> dict:
        return self._rows[0]


class _Conn:
    def __init__(self, fills: list[dict]) -> None:
        self.fills = fills
        self.sql: list[str] = []
        self.topics: list[str] = []
        self.committed = False

    def cursor(self, **_kw: object) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.committed = True


def _five_closed() -> list[dict]:
    out: list[dict] = []
    for i in range(5):
        at = T0 + timedelta(days=10 * i)
        out += [
            _fill(f"K{i}", "ZZZ", "SLD", 1, 2.0, at),
            _fill(f"K{i}", "ZZZ", "BOT", 1, 0.5, at + timedelta(days=4)),
        ]
    return out


def _distill(monkeypatch, flex: tuple[str, str] | None) -> tuple[dict, _Conn]:
    import bifrost_research.auth.bearer as bearer
    from bifrost_research.engines.journal_distill import run_distill

    monkeypatch.setattr(bearer, "known_owner_ids", lambda: ("alice",))
    conn = _Conn(_five_closed())
    return run_distill(conn, today=date(2026, 3, 2), flex=flex), conn


def test_distill_reads_the_fills_when_flex_is_ok(monkeypatch) -> None:
    result, conn = _distill(monkeypatch, ("ok", "2 kinds fresh"))
    assert result["fills"] == "read" and "fills_skip_reason" not in result
    assert any("raw_broker.executions_final" in s for s in conn.sql)
    assert "axis-hold" in conn.topics


@pytest.mark.parametrize("verdict", ["failed", "stale", "unknown"])
def test_distill_skips_the_fills_when_flex_is_not_fresh(monkeypatch, verdict: str) -> None:
    """The night the Flex ingest failed: no memory from the stale book, a reason, no failure."""
    result, conn = _distill(monkeypatch, (verdict, "flex-trades: [1003]"))
    assert result["fills"] == "skipped" and result["flex_ingest"] == verdict
    assert result["fills_skip_reason"] == f"Flex ingest {verdict}: flex-trades: [1003]"
    assert not any("raw_broker" in s for s in conn.sql)
    assert "axis-hold" not in conn.topics
    # The sources that do not read Flex still ran, and the night committed.
    assert any("research.ai_draft" in s for s in conn.sql)
    assert conn.committed


def test_distill_asks_the_flex_gate_when_not_told(monkeypatch) -> None:
    import bifrost_research.orchestration.flex_husbandry as fh

    monkeypatch.setattr(fh, "flex_ingest_now", lambda: ("failed", "flex-trades: [1003]"))
    result, conn = _distill(monkeypatch, None)
    assert result["fills"] == "skipped"
    assert not any("raw_broker" in s for s in conn.sql)
