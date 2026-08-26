"""AI Forecast Engine — intraday playbook, hourly path call, option scaffolds (Wave 4.2).

D10 BLOCKED — structures are advisory; no order placement.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from bifrost_research.db.upsert import batch_upsert
from bifrost_research.engines.forecast.llm import (
    LLMProvider,
    enrich_with_llm,
    get_default_provider,
)
from bifrost_research.engines.forecast.terrain import MarketTerrain, Regime

_SESSION_COLS = (
    "session_id",
    "symbol",
    "trade_date",
    "regime",
    "spot",
    "prob_rangy",
    "prob_bull",
    "prob_bear",
    "prob_squeeze",
    "expected_close",
    "structures_json",
    "narrative",
    "llm_provider",
    "terrain_json",
    "advisory",
    "computed_at",
)

_HOURLY_COLS = (
    "session_id",
    "symbol",
    "trade_date",
    "hour_et",
    "path_call",
    "level_low",
    "level_high",
    "level_target",
    "confidence",
    "notes",
    "computed_at",
)

# Regular-session hourly anchors (ET), scaffolding for Path Call
_SESSION_HOURS = (10, 11, 12, 13, 14, 15)


@dataclass(frozen=True)
class ScenarioProbabilities:
    rangy: float
    bull: float
    bear: float
    squeeze: float

    def normalized(self) -> ScenarioProbabilities:
        total = self.rangy + self.bull + self.bear + self.squeeze
        if total <= 0:
            return ScenarioProbabilities(0.4, 0.2, 0.2, 0.2)
        r = round(self.rangy / total, 4)
        b = round(self.bull / total, 4)
        be = round(self.bear / total, 4)
        sq = round(1.0 - r - b - be, 4)
        if sq < 0:
            sub = r + b + be or 1.0
            r = round(r / sub, 4)
            b = round(b / sub, 4)
            be = round(1.0 - r - b, 4)
            sq = 0.0
        return ScenarioProbabilities(r, b, be, sq)

    def to_dict(self) -> dict[str, float]:
        n = self.normalized()
        return {
            "rangy": n.rangy,
            "bull": n.bull,
            "bear": n.bear,
            "squeeze": n.squeeze,
        }


@dataclass(frozen=True)
class HourlyPathCall:
    hour_et: int
    path_call: str
    level_low: float
    level_high: float
    level_target: float
    confidence: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptionStructureRec:
    structure: str  # Butterfly / Iron Condor / Bull Call Vertical
    legs_summary: str
    pop: float  # Probability of Profit scaffold 0–1
    ev: float  # Expected value scaffold (relative)
    cvar: float  # Conditional VaR scaffold (negative = loss)
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ForecastSession:
    session_id: str
    symbol: str
    trade_date: date
    regime: Regime
    spot: float
    scenarios: ScenarioProbabilities
    expected_close: float
    hourly: list[HourlyPathCall] = field(default_factory=list)
    structures: list[OptionStructureRec] = field(default_factory=list)
    narrative: str = ""
    llm_provider: str = "heuristic"
    terrain_json: dict[str, Any] = field(default_factory=dict)
    advisory: str = "D10 BLOCKED — advisory only; no order placement"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "regime": self.regime,
            "spot": self.spot,
            "scenarios": self.scenarios.to_dict(),
            "expected_close": self.expected_close,
            "hourly": [h.to_dict() for h in self.hourly],
            "structures": [s.to_dict() for s in self.structures],
            "narrative": self.narrative,
            "llm_provider": self.llm_provider,
            "terrain_json": self.terrain_json,
            "advisory": self.advisory,
        }


def scenario_probabilities_from_terrain(terrain: MarketTerrain) -> ScenarioProbabilities:
    """Map terrain scores into 4-scenario probability mass."""
    regime = terrain.regime
    pin = terrain.pin_score
    trend = terrain.trend_release
    squeeze = terrain.vol_squeeze
    tail = terrain.tail_risk

    if regime == "crash-risk":
        raw = ScenarioProbabilities(
            rangy=15 + pin * 0.1,
            bull=5,
            bear=40 + tail * 0.35,
            squeeze=10 + squeeze * 0.15,
        )
    elif regime == "trending":
        bullish = trend >= 55
        raw = ScenarioProbabilities(
            rangy=15 + pin * 0.15,
            bull=(35 + trend * 0.35) if bullish else (10 + (100 - trend) * 0.1),
            bear=(10 + (100 - trend) * 0.1) if bullish else (35 + (100 - trend) * 0.25),
            squeeze=10 + squeeze * 0.2,
        )
    else:  # range
        raw = ScenarioProbabilities(
            rangy=35 + pin * 0.35,
            bull=15 + max(0.0, trend - 50) * 0.3,
            bear=15 + max(0.0, tail - 40) * 0.25,
            squeeze=20 + squeeze * 0.25,
        )
    return raw.normalized()


def build_hourly_path_calls(
    *,
    spot: float,
    expected_close: float,
    gamma_zone_low: float,
    gamma_zone_high: float,
    scenarios: ScenarioProbabilities,
    regime: Regime,
) -> list[HourlyPathCall]:
    """Scaffold hourly Path Call + expected levels through the cash session."""
    n = scenarios.normalized()
    dominant = max(
        (("rangy", n.rangy), ("bull", n.bull), ("bear", n.bear), ("squeeze", n.squeeze)),
        key=lambda x: x[1],
    )[0]
    lo, hi = gamma_zone_low, gamma_zone_high
    if lo > hi:
        lo, hi = hi, lo
    mid = (lo + hi) / 2.0
    out: list[HourlyPathCall] = []
    for i, hour in enumerate(_SESSION_HOURS):
        t = (i + 1) / len(_SESSION_HOURS)
        # Interpolate target from spot toward expected_close
        target = spot + (expected_close - spot) * t
        if dominant == "bull":
            path = "higher-high"
            band = (mid - (mid - lo) * 0.3, hi)
        elif dominant == "bear":
            path = "lower-low"
            band = (lo, mid + (hi - mid) * 0.3)
        elif dominant == "squeeze":
            path = "coil"
            band = (spot * 0.995, spot * 1.005)
        else:
            path = "mean-revert"
            band = (lo, hi)
        # Late day: pull harder to expected close
        if hour >= 14:
            target = 0.4 * target + 0.6 * expected_close
            path = f"{path}->close"
        conf = round(0.45 + 0.4 * {
            "rangy": n.rangy,
            "bull": n.bull,
            "bear": n.bear,
            "squeeze": n.squeeze,
        }[dominant], 3)
        notes = f"regime={regime}; dominant={dominant}"
        out.append(
            HourlyPathCall(
                hour_et=hour,
                path_call=path,
                level_low=round(band[0], 4),
                level_high=round(band[1], 4),
                level_target=round(target, 4),
                confidence=min(0.95, conf),
                notes=notes,
            )
        )
    return out


def recommend_option_structures(
    *,
    spot: float,
    scenarios: ScenarioProbabilities,
    gamma_zone_low: float,
    gamma_zone_high: float,
    regime: Regime,
) -> list[OptionStructureRec]:
    """Heuristic structure menu with PoP / EV / CVaR scaffolding (not calibrated)."""
    n = scenarios.normalized()
    width = max(abs(gamma_zone_high - gamma_zone_low), spot * 0.01)
    wing = round(width / 2.0, 2)
    strike_atm = round(spot)
    recs: list[OptionStructureRec] = []

    # Iron Condor — favored in rangy / high pin
    ic_pop = round(0.35 + 0.45 * n.rangy, 3)
    ic_ev = round((ic_pop - 0.5) * 0.4, 3)
    ic_cvar = round(-0.8 * (1.0 - ic_pop), 3)
    recs.append(
        OptionStructureRec(
            structure="Iron Condor",
            legs_summary=(
                f"short put {strike_atm - wing}/ long put {strike_atm - 2 * wing}; "
                f"short call {strike_atm + wing}/ long call {strike_atm + 2 * wing}"
            ),
            pop=ic_pop,
            ev=ic_ev,
            cvar=ic_cvar,
            rationale="Favored when Rangy / pin regime; short premium inside gamma zone",
        )
    )

    # Butterfly — squeeze / pin
    bf_pop = round(0.30 + 0.40 * max(n.squeeze, n.rangy * 0.8), 3)
    bf_ev = round((bf_pop - 0.48) * 0.5, 3)
    bf_cvar = round(-0.6 * (1.0 - bf_pop), 3)
    recs.append(
        OptionStructureRec(
            structure="Butterfly",
            legs_summary=(
                f"long 1x {strike_atm - wing} / short 2x {strike_atm} / long 1x {strike_atm + wing}"
            ),
            pop=bf_pop,
            ev=bf_ev,
            cvar=bf_cvar,
            rationale="Favored on Vol Squeeze / pin into expected close",
        )
    )

    # Bull Call Vertical — trending bull
    bc_pop = round(0.35 + 0.45 * n.bull, 3)
    if regime == "crash-risk":
        bc_pop = round(bc_pop * 0.7, 3)
    bc_ev = round((bc_pop - 0.5) * 0.55, 3)
    bc_cvar = round(-1.0 * (1.0 - bc_pop), 3)
    recs.append(
        OptionStructureRec(
            structure="Bull Call Vertical",
            legs_summary=f"long call {strike_atm} / short call {strike_atm + wing}",
            pop=bc_pop,
            ev=bc_ev,
            cvar=bc_cvar,
            rationale="Directional debit vertical when Bull scenario mass dominates",
        )
    )
    # Sort by EV descending for display
    return sorted(recs, key=lambda r: r.ev, reverse=True)


def build_forecast_session(
    terrain: MarketTerrain,
    *,
    session_id: str | None = None,
    llm: LLMProvider | None = None,
    enrich: bool = True,
) -> ForecastSession:
    """Build full forecast session from a MarketTerrain snapshot."""
    scenarios = scenario_probabilities_from_terrain(terrain)
    hourly = build_hourly_path_calls(
        spot=terrain.spot,
        expected_close=terrain.expected_close,
        gamma_zone_low=terrain.gamma_zone_low,
        gamma_zone_high=terrain.gamma_zone_high,
        scenarios=scenarios,
        regime=terrain.regime,
    )
    structures = recommend_option_structures(
        spot=terrain.spot,
        scenarios=scenarios,
        gamma_zone_low=terrain.gamma_zone_low,
        gamma_zone_high=terrain.gamma_zone_high,
        regime=terrain.regime,
    )
    provider = llm or get_default_provider()
    narrative = ""
    provider_name = provider.name
    if enrich:
        enriched = enrich_with_llm(
            provider,
            {
                "symbol": terrain.symbol,
                "regime": terrain.regime,
                "scenarios": scenarios.to_dict(),
                "terrain": {
                    "pin_score": terrain.pin_score,
                    "trend_release": terrain.trend_release,
                    "vol_squeeze": terrain.vol_squeeze,
                    "tail_risk": terrain.tail_risk,
                },
            },
        )
        narrative = str(enriched.get("narrative") or "")
        provider_name = str(enriched.get("provider") or provider.name)

    sid = session_id or f"{terrain.symbol}-{terrain.trade_date.isoformat()}-{uuid.uuid4().hex[:8]}"
    return ForecastSession(
        session_id=sid,
        symbol=terrain.symbol,
        trade_date=terrain.trade_date,
        regime=terrain.regime,
        spot=terrain.spot,
        scenarios=scenarios,
        expected_close=terrain.expected_close,
        hourly=hourly,
        structures=structures,
        narrative=narrative,
        llm_provider=provider_name,
        terrain_json=terrain.to_dict(),
    )


_HOURLY_SESSION_COLS = (
    "hourly_session_id",
    "parent_session_id",
    "symbol",
    "trade_date",
    "hour_et",
    "regime",
    "spot",
    "prob_rangy",
    "prob_bull",
    "prob_bear",
    "prob_squeeze",
    "expected_close",
    "structures_json",
    "narrative",
    "llm_provider",
    "terrain_json",
    "advisory",
    "computed_at",
)


def upsert_hourly_sessions(conn: Any, session: ForecastSession) -> int:
    """Write one hourly-session row per path-call hour (Wave R4)."""
    n = session.scenarios.normalized()
    now = datetime.now(timezone.utc)
    rows: list[tuple[Any, ...]] = []
    for h in session.hourly:
        sid = f"{session.session_id}-h{int(h.hour_et):02d}"
        terrain = dict(session.terrain_json or {})
        terrain["llm_tokens"] = terrain.get("llm_tokens") or {}
        rows.append(
            (
                sid,
                session.session_id,
                session.symbol,
                session.trade_date,
                int(h.hour_et),
                session.regime,
                session.spot,
                n.rangy,
                n.bull,
                n.bear,
                n.squeeze,
                h.level_target,
                [s.to_dict() for s in session.structures],
                h.path_call,
                session.llm_provider,
                terrain,
                session.advisory,
                now,
            )
        )
    if not rows:
        return 0
    return batch_upsert(
        conn,
        "features.stock_forecast_hourly_session",
        _HOURLY_SESSION_COLS,
        rows,
        conflict_keys=("hourly_session_id",),
        set_fetched_at=False,
    )


def upsert_forecast_session(conn: Any, session: ForecastSession) -> int:
    n = session.scenarios.normalized()
    now = datetime.now(timezone.utc)
    batch_upsert(
        conn,
        "features.stock_forecast_session",
        _SESSION_COLS,
        [
            (
                session.session_id,
                session.symbol,
                session.trade_date,
                session.regime,
                session.spot,
                n.rangy,
                n.bull,
                n.bear,
                n.squeeze,
                session.expected_close,
                [s.to_dict() for s in session.structures],
                session.narrative,
                session.llm_provider,
                session.terrain_json,
                session.advisory,
                now,
            )
        ],
        conflict_keys=("session_id",),
        set_fetched_at=False,
    )
    hourly_rows = [
        (
            session.session_id,
            session.symbol,
            session.trade_date,
            h.hour_et,
            h.path_call,
            h.level_low,
            h.level_high,
            h.level_target,
            h.confidence,
            h.notes,
            now,
        )
        for h in session.hourly
    ]
    batch_upsert(
        conn,
        "features.stock_forecast_hourly",
        _HOURLY_COLS,
        hourly_rows,
        conflict_keys=("session_id", "hour_et"),
        set_fetched_at=False,
    )
    upsert_hourly_sessions(conn, session)
    return 1 + len(hourly_rows)
