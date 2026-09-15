"""Beta / correlation / RV cone pure functions, pinned to hand-computed samples (R9 F4)."""

from __future__ import annotations

import math

from bifrost_research.engines.risk_stats import (
    aligned_returns,
    beta,
    correlation,
    current_realised_vol,
    log_returns,
    percentile,
    realised_vol,
    rolling_realised_vol,
    rv_cone,
    sufficient,
)


def test_log_returns_skip_broken_closes() -> None:
    assert log_returns([100.0, 110.0]) == [math.log(1.1)]
    # A missing or non-positive close breaks the chain instead of splicing a return
    # across it: 100 → 121 must never appear as one day's move.
    assert log_returns([100.0, None, 121.0]) == []
    assert log_returns([100.0, 0.0, 121.0]) == []
    assert log_returns([]) == []


def test_beta_of_a_series_that_moves_twice_the_benchmark_is_two() -> None:
    bench = [0.01, -0.01, 0.02, -0.02]
    asset = [2 * r for r in bench]
    assert beta(asset, bench) is not None
    assert abs(beta(asset, bench) - 2.0) < 1e-12
    # cov / var by hand on an asymmetric sample.
    a, b = [0.02, 0.00, 0.01], [0.01, 0.01, -0.01]
    ma, mb = sum(a) / 3, sum(b) / 3
    expect = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / sum((y - mb) ** 2 for y in b)
    assert abs(beta(a, b) - expect) < 1e-12
    # A benchmark that never moved has no beta, and one point is not a sample.
    assert beta([0.01, 0.02], [0.0, 0.0]) is None
    assert beta([0.01], [0.01]) is None


def test_correlation_is_one_minus_one_and_none() -> None:
    up = [0.01, 0.02, 0.03, 0.015]
    assert abs(correlation(up, up) - 1.0) < 1e-12
    assert abs(correlation(up, [-x for x in up]) + 1.0) < 1e-12
    assert correlation(up, [0.01] * 4) is None
    assert correlation([0.01], [0.02]) is None


def test_pairs_are_built_on_the_date_intersection_not_on_padded_series() -> None:
    left = {"d1": 100.0, "d2": 101.0, "d3": 102.0, "d4": 103.0}
    right = {"d1": 50.0, "d3": 51.0, "d4": 52.0}
    l_ret, r_ret, dates = aligned_returns(left, right)
    # d2 is missing on the right, so both sides skip it — three shared dates, two returns.
    assert dates == ["d3", "d4"]
    assert len(l_ret) == len(r_ret) == 2
    assert abs(l_ret[0] - math.log(102 / 100)) < 1e-12
    assert abs(r_ret[0] - math.log(51 / 50)) < 1e-12


def test_realised_vol_annualises_with_root_252() -> None:
    returns = [0.01, -0.01, 0.01, -0.01]
    # Mean 0, population sigma 0.01 → 0.01 * sqrt(252).
    assert abs(realised_vol(returns) - 0.01 * math.sqrt(252)) < 1e-12
    assert realised_vol([0.01]) is None
    assert rolling_realised_vol(returns, 2) and len(rolling_realised_vol(returns, 2)) == 3
    assert rolling_realised_vol(returns, 9) == []
    assert abs(current_realised_vol(returns, 4) - realised_vol(returns)) < 1e-12
    assert current_realised_vol(returns, 9) is None


def test_percentile_matches_percentile_cont_interpolation() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    assert percentile(xs, 0.5) == 2.5
    assert abs(percentile(xs, 0.05) - 1.15) < 1e-12
    assert percentile(xs, 0.0) == 1.0 and percentile(xs, 1.0) == 4.0
    assert percentile([], 0.5) is None
    assert percentile([7.0], 0.2) == 7.0


def test_rv_cone_reports_every_tenor_with_its_sample() -> None:
    returns = [0.01, -0.01] * 30
    rows = rv_cone(returns, [10, 20, 400])
    by_days = {r["days"]: r for r in rows}
    assert by_days[10]["n"] == 51 and by_days[20]["n"] == 41
    # A tenor longer than the history is answered as empty, not as a shorter window.
    assert by_days[400]["n"] == 0 and by_days[400]["p50"] is None
    assert by_days[10]["p05"] <= by_days[10]["p50"] <= by_days[10]["p95"]


def test_a_window_under_eighty_percent_filled_is_not_that_window() -> None:
    assert sufficient(202, 252) and sufficient(252, 252)
    assert not sufficient(201, 252)
    assert sufficient(48, 60) and not sufficient(47, 60)
