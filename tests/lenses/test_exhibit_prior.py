"""``ExhibitResponse.prior`` — the same lens one session earlier, banded by the registry (R9 F2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from bifrost_research.lenses.exhibit_prior import NO_PRIOR_LENSES, ROW_PRIOR_LENSES, prior_reading
from bifrost_research.lenses.exhibits import build_exhibit
from bifrost_research.lenses.registry import LENSES

NOW = datetime.now(timezone.utc)
TODAY = date(2026, 9, 4)
YESTERDAY = date(2026, 9, 3)

Route = tuple[Callable[[str], bool], list[tuple[Any, ...]]]


class _Cur:
    """Cursor whose routes may answer differently for the prior query (``trade_date < ``)."""

    def __init__(self, routes: list[Route]) -> None:
        self._routes = routes
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        for pred, rows in self._routes:
            if pred(sql):
                self._rows = list(rows)
                return
        self._rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, routes: list[Route]) -> None:
        self.routes = routes

    def cursor(self) -> _Cur:
        return _Cur(self.routes)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _is_prior(sql: str) -> bool:
    return "trade_date < %s::date" in sql


def test_row_prior_reads_the_previous_session_and_bands_it() -> None:
    routes: list[Route] = [
        (lambda sql: "option_metric_iv_percentile_daily" in sql and _is_prior(sql), [(YESTERDAY, 35.0)]),
        (lambda sql: "option_metric_iv_percentile_daily" in sql, [(TODAY, 85.0, 84.0, 0.51, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "iv_rank", "NVDA")
    assert exh.readings["iv_rank_1y"] == 85.0
    assert exh.verdict is not None and exh.verdict["band"] == "hot"
    # Two days of data: the prior sits strictly earlier and carries its own band.
    assert exh.prior == {"as_of": "2026-09-03", "value": 35.0, "band": "lean_cold"}
    assert exh.prior["as_of"] < exh.as_of


def test_one_day_of_data_has_no_prior() -> None:
    routes: list[Route] = [
        (lambda sql: "option_metric_iv_percentile_daily" in sql and _is_prior(sql), []),
        (lambda sql: "option_metric_iv_percentile_daily" in sql, [(TODAY, 85.0, 84.0, 0.51, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "iv_rank", "NVDA")
    assert exh.as_of == "2026-09-04" and exh.prior is None


def test_categorical_prior_uses_the_registry_categories() -> None:
    routes: list[Route] = [
        (lambda sql: "stock_forecast_terrain_daily" in sql and _is_prior(sql), [(YESTERDAY, "crash-risk")]),
        (lambda sql: "stock_forecast_terrain_daily" in sql,
         [(TODAY, "range", 40.0, 74.7, 83.3, 7.0, 225.4, 230.36, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "terrain", "NVDA")
    assert exh.verdict is not None and exh.verdict["band"] == "neutral"
    assert exh.prior == {"as_of": "2026-09-03", "value": "crash-risk", "band": "hot"}


def test_gex_prior_is_the_sign_of_net_gamma_on_the_previous_date() -> None:
    routes: list[Route] = [
        (lambda sql: "option_metric_gex_levels_daily" in sql and _is_prior(sql),
         [(YESTERDAY, date(2026, 10, 2), 228.0, 4.0e8, 226.0, 240.0, 220.0, NOW)]),
        (lambda sql: "option_metric_gex_levels_daily" in sql,
         [(TODAY, date(2026, 10, 2), 230.0, -5.0e8, 236.0, 240.0, 220.0, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "gex_regime", "NVDA")
    assert exh.readings["regime"] == "negative"
    assert exh.prior == {"as_of": "2026-09-03", "value": "positive", "band": "cold"}


def test_built_prior_recomputes_skew_percentile_for_the_previous_date() -> None:
    routes: list[Route] = [
        # The percentile query is shared, so the prior day's |slope| is judged the same way.
        (lambda sql: "option_surface_fit_daily" in sql and "COUNT(*)" in sql, [(120, 0.09, 12.0)]),
        (lambda sql: "option_surface_fit_daily" in sql and _is_prior(sql),
         [(YESTERDAY, date(2026, 10, 2), 28, 0.40, -0.10, 0.004, 14, NOW)]),
        (lambda sql: "option_surface_fit_daily" in sql,
         [(TODAY, date(2026, 10, 2), 28, 0.41, -0.30, 0.004, 14, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "skew", "NVDA")
    assert exh.prior is not None and exh.prior["as_of"] == "2026-09-03"
    assert exh.prior["value"] == 12.0 and exh.prior["band"] == "cold"


def test_term_slope_prior_is_near_minus_far_on_the_previous_date() -> None:
    routes: list[Route] = [
        (lambda sql: "option_surface_fit_daily" in sql and _is_prior(sql),
         [(YESTERDAY, date(2026, 10, 2), 28, 0.36, NOW), (YESTERDAY, date(2026, 12, 18), 105, 0.40, NOW)]),
        (lambda sql: "option_surface_fit_daily" in sql,
         [(TODAY, date(2026, 10, 2), 28, 0.41, NOW), (TODAY, date(2026, 12, 18), 105, 0.38, NOW)]),
    ]
    exh = build_exhibit(_Conn(routes), "term_slope", "NVDA")
    assert exh.verdict is not None and exh.verdict["band"] == "hot"
    assert exh.prior is not None and abs(exh.prior["value"] - (0.36 - 0.40)) < 1e-9
    assert exh.prior["band"] == "cold"  # contango yesterday, backwardation today


def test_opex_pin_prior_uses_the_close_on_its_own_date() -> None:
    routes: list[Route] = [
        (lambda sql: "option_metric_max_pain_daily" in sql and _is_prior(sql),
         [(YESTERDAY, date(2026, 9, 18), 230.0, 480_000, NOW, 15)]),
        (lambda sql: "option_metric_max_pain_daily" in sql, [(TODAY, date(2026, 9, 18), 230.0, 500_000, NOW, 14)]),
        (lambda sql: "raw_market.stock_daily" in sql and "bar_date = %s" in sql, [(231.0,)]),
    ]
    exh = build_exhibit(_Conn(routes), "opex_pin", "NVDA")
    assert exh.prior is not None and exh.prior["as_of"] == "2026-09-03"
    assert abs(exh.prior["value"] - (1.0 / 231.0)) < 1e-9 and exh.prior["band"] == "hot"


def test_order_sentiment_prior_without_tape_carries_no_band() -> None:
    routes: list[Route] = [
        (lambda sql: "option_flow_sentiment_daily" in sql and _is_prior(sql),
         [(YESTERDAY, 48.0, "option_snapshot_aggregates")]),
        (lambda sql: "option_flow_sentiment_daily" in sql,
         [(TODAY, 45.0, 0.5, 0.9, 2e6, 1e6, NOW, "option_trades_tape")]),
    ]
    exh = build_exhibit(_Conn(routes), "order_sentiment", "SPY")
    assert exh.verdict is not None and exh.verdict["band"] == "hot"
    assert exh.prior == {"as_of": "2026-09-03", "value": 48.0, "band": None}


def test_forecast_path_has_no_prior_and_every_other_lens_is_covered() -> None:
    assert prior_reading(_Conn([]), "forecast_path", "SPY", "2026-09-04") is None
    from bifrost_research.lenses.exhibit_prior import _BUILT_PRIORS

    covered = set(ROW_PRIOR_LENSES) | set(_BUILT_PRIORS) | set(NO_PRIOR_LENSES)
    assert covered == set(LENSES)


def test_a_failing_prior_query_is_a_caveat_not_a_failed_exhibit() -> None:
    class _Boom(_Conn):
        def cursor(self) -> Any:
            raise RuntimeError("prior blew up")

    from bifrost_research.lenses.exhibit_prior import attach_prior

    routes: list[Route] = [(lambda sql: True, [(TODAY, 85.0, 84.0, 0.51, NOW)])]
    exh = build_exhibit(_Conn(routes), "iv_rank", "NVDA")
    assert exh.readings["iv_rank_1y"] == 85.0

    attach_prior(_Boom([]), exh, "iv_rank")
    assert exh.prior is None
    assert any("Previous-session reading" in c for c in exh.caveats)
