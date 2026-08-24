"""Pure-compute tests for Momentum Radar (no DB)."""

from __future__ import annotations

from datetime import date, timedelta

from bifrost_research.engines.momentum.radar import (
    DailyBar,
    grade_from_score,
    path_from_factors,
    score_momentum,
)


def _bars(
    n: int = 60,
    *,
    start: float = 100.0,
    drift: float = 0.002,
    volume: float = 1_000_000,
) -> list[DailyBar]:
    out: list[DailyBar] = []
    px = start
    d0 = date(2024, 1, 2)
    for i in range(n):
        o = px
        c = px * (1 + drift)
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        vwap = (o + c) / 2
        out.append(
            DailyBar(
                bar_date=d0 + timedelta(days=i),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=volume * (1.0 + 0.01 * (i % 5)),
                vwap=vwap,
            )
        )
        px = c
    return out


def test_grade_thresholds() -> None:
    assert grade_from_score(90) == "A+"
    assert grade_from_score(80) == "A"
    assert grade_from_score(65) == "B"
    assert grade_from_score(50) == "C"
    assert grade_from_score(40) == "D"


def test_score_uptrend_strong() -> None:
    result = score_momentum(_bars(80, drift=0.004))
    assert 0 <= result["score"] <= 100
    assert result["grade"] in {"A+", "A", "B", "C", "D"}
    assert result["path"] in {"EXT", "PB", "FAIL", "HALT"}
    assert result["factors"]["z_ofi"] == 50.0  # stub when unavailable
    assert result["z_ofi_available"] is False
    assert "z_sdt" in result["factors"]


def test_z_ofi_override() -> None:
    result = score_momentum(_bars(40), z_ofi=80.0)
    assert result["factors"]["z_ofi"] == 80.0
    assert result["z_ofi_available"] is True


def test_path_halt_on_crash() -> None:
    path = path_from_factors(
        score=70, h_52w=80, a_factor=60, crash=20, accept_vwap=60
    )
    assert path == "HALT"


def test_path_ext() -> None:
    path = path_from_factors(
        score=80, h_52w=90, a_factor=70, crash=80, accept_vwap=70
    )
    assert path == "EXT"
