"""signal_hit walks to the New York session the batch closed (TD-156).

It ran on its own 00:10 UTC schedule, two hours before research_trading_day wrote
the session's features, and dated its walk by ``ny_today``. Inside the batch the
walk ends at ``latest_closed_session`` and the asset's output check fails when
the newest day walked is not that session.
"""

from __future__ import annotations

from datetime import date

import pytest

from bifrost_research.engines.signal_hit import entry

SESSION = date(2026, 10, 2)  # a Friday


class _Conn:
    def close(self) -> None:
        pass


def _stub(monkeypatch: pytest.MonkeyPatch, days: list[date]) -> dict[str, object]:
    seen: dict[str, object] = {}
    monkeypatch.setattr(entry, "connect", lambda: _Conn())
    monkeypatch.setattr(entry, "latest_closed_session", lambda conn: SESSION)

    def fake_days(conn, start, end):
        seen["end"] = end
        return [d for d in days if d <= end]

    monkeypatch.setattr(entry, "_trading_days", fake_days)
    monkeypatch.setattr(entry, "_watchlist", lambda: [])
    monkeypatch.setattr(entry, "build_rows_for_day", lambda c, d, lenses, symbols=None: [])
    monkeypatch.setattr(entry, "lenses_with_source", lambda c, d, lenses: [])
    monkeypatch.setattr(entry, "replace_unfired", lambda *a, **k: 0)
    return seen


def test_the_walk_ends_at_the_latest_closed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [date(2026, 9, 30), date(2026, 10, 1), SESSION])
    out = entry.run(lookback_days=3)
    assert seen["end"] == SESSION
    assert out["session"] == out["as_of"] == SESSION.isoformat()


def test_an_explicit_as_of_is_not_held_to_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [date(2026, 9, 30), date(2026, 10, 1)])
    out = entry.run(lookback_days=2, as_of=date(2026, 10, 1))
    assert out["session"] is None and out["as_of"] == "2026-10-01"


def test_the_output_check_fails_when_the_walk_misses_the_session() -> None:
    pytest.importorskip("dagster")
    from bifrost_research.orchestration.asset_checks import judge_output
    from bifrost_research.orchestration.research_aux_schedules import SIGNAL_HIT_SPEC

    base = {"rows_written": 40}
    stale, _ = judge_output({**base, "as_of": "2026-10-01", "session": "2026-10-02"}, SIGNAL_HIT_SPEC)
    assert stale and "2026-10-02" in stale[0][1]
    fresh, _ = judge_output({**base, "as_of": "2026-10-02", "session": "2026-10-02"}, SIGNAL_HIT_SPEC)
    assert fresh == []
    by_hand, _ = judge_output({**base, "as_of": "2026-10-01", "session": None}, SIGNAL_HIT_SPEC)
    assert by_hand == []
