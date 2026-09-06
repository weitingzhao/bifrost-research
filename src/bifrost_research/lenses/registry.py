"""Lens registry — the one place a reading becomes hot / cold / neutral.

Before this module the same idea lived in four places with three answers:
IV Radar called IV Rank ≥ 60 a *sell premium bias*, Signal Decay and Scan
classified *hot* at ≥ 80, and the alert engine kept its own tuple of which
lenses have a track record at all. Program ``research-loop-automation`` Phase A1
moves every such number here. Consumers:

- ``engines/signal_hit`` — trigger sides (``trigger_side``)
- ``engines/scan`` — ``lens_flags`` on the scan rows (``scan_flag``)
- ``engines/alert_scan`` — which lenses carry a settled track record (``decay_lens_ids``)
- ``api/similar_regime`` — which lens ids the k-NN accepts (``similar_lens_ids``)
- ``api/lenses`` + MCP ``research.lenses.list`` — the Trade frontend and Copilot read
  the same bands the engines apply, so a page verdict and a hit-rate can never
  disagree about what "hot" meant.

Bands on the 0–100 score scale (the engines' truth, kept as-is):

    hot ≥ 80 · lean_hot (60, 80) · neutral [40, 60] · lean_cold (20, 40) · cold ≤ 20

The lean bands exist for page wording ("no standalone edge — wait for
confirmation"); Scan and Signal Decay flag only hot / cold / neutral, exactly as
they did before. Each ``LensSpec`` also carries the aliases the older contracts
use (``scan_flag`` key, ``decay_lens`` value, ``similar_lens`` param) so nothing
downstream renames. D10 BLOCKED — advisory classification only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

REGISTRY_VERSION = 1

Band = Literal["hot", "lean_hot", "neutral", "lean_cold", "cold"]
Kind = Literal["score", "signed", "severity", "distance", "categorical"]
# How Signal Decay scores a trigger of this lens against the forward return:
#   mean_revert  hot expects the price down, cold expects it up (the Wave I rule)
#   follow       hot expects up, cold expects down (flow that leans long is followed)
#   magnitude    hot expects a large absolute move, cold a small one (gamma regimes)
#   none         the lens is not a decay lens
HitRule = Literal["mean_revert", "follow", "magnitude", "none"]

# The 0–100 score bands every score-kind lens shares.
SCORE_HOT = 80.0
SCORE_LEAN_HOT = 60.0
SCORE_LEAN_COLD = 40.0
SCORE_COLD = 20.0


@dataclass(frozen=True)
class Bands:
    """Thresholds on the lens' own scale; ``LensSpec.kind`` says how to read them.

    score / signed: ``value >= hot`` → hot · ``value <= cold`` → cold, lean bands
    between. severity: ``abs(value) >= hot`` → hot, ``>= lean_hot`` → lean_hot.
    distance: ``abs(value) <= hot`` → hot (near the magnet), else neutral.
    """

    hot: float | None = None
    lean_hot: float | None = None
    lean_cold: float | None = None
    cold: float | None = None


SCORE_BANDS = Bands(hot=SCORE_HOT, lean_hot=SCORE_LEAN_HOT, lean_cold=SCORE_LEAN_COLD, cold=SCORE_COLD)


@dataclass(frozen=True)
class LensSpec:
    id: str
    label: str
    kind: Kind
    source_table: str
    value_column: str | None
    unit: str
    bands: Bands
    hot_means: str
    cold_means: str
    horizons: tuple[int, ...]
    page_route: str
    scan_flag: str | None = None
    decay_lens: str | None = None
    similar_lens: str | None = None
    data_dependency: str | None = None
    notes: str = ""
    # Categorical lenses: (category value, band) pairs; empty for numeric lenses.
    categories: tuple[tuple[str, str], ...] = ()
    hit_rule: HitRule = "none"
    # For magnitude lenses: the absolute 5d / 20d move that counts as "large".
    move_threshold: tuple[float, float] | None = None


_SPECS: tuple[LensSpec, ...] = (
    LensSpec(
        id="iv_rank",
        label="IV Rank",
        kind="score",
        source_table="features.option_metric_iv_percentile_daily",
        value_column="iv_rank_1y",
        unit="pct_of_1y_range",
        bands=SCORE_BANDS,
        hot_means="Implied vol near its 1y high — premium is rich; short-premium bias.",
        cold_means="Implied vol near its 1y low — premium is cheap; long-premium bias.",
        horizons=(5, 20),
        page_route="/research/vol-regime?view=iv-rank",
        scan_flag="iv_rank",
        decay_lens="iv_rank",
        similar_lens="iv_rank",
        hit_rule="mean_revert",
    ),
    LensSpec(
        id="iv_percentile",
        label="IV Percentile",
        kind="score",
        source_table="features.option_metric_iv_percentile_daily",
        value_column="iv_percentile_1y",
        unit="pct_of_days_below",
        bands=SCORE_BANDS,
        hot_means="Most of the last year traded with lower IV than today.",
        cold_means="Most of the last year traded with higher IV than today.",
        horizons=(5, 20),
        page_route="/research/vol-regime?view=iv-rank",
        notes="Companion column to IV Rank; not a standalone trigger.",
    ),
    LensSpec(
        id="vrp",
        label="IV-RV Spread (VRP)",
        kind="score",
        source_table="features.stock_signal_vrp_daily",
        value_column="vrp_pct_252d",
        unit="pctile_252d",
        bands=SCORE_BANDS,
        hot_means="Implied vol far above realised — the seller is paid; sell-vol edge.",
        cold_means="Implied vol at or below realised — buying vol is cheap.",
        horizons=(5, 20),
        page_route="/research/vol-regime?view=vrp",
        scan_flag="vrp",
        decay_lens="vrp",
        similar_lens="vrp",
        hit_rule="mean_revert",
    ),
    LensSpec(
        id="skew",
        label="Skew (ATM slope)",
        kind="severity",
        source_table="features.option_surface_fit_daily",
        value_column="atm_slope",
        unit="slope_at_30dte",
        bands=Bands(hot=0.25, lean_hot=0.12),
        hot_means="Skew extreme — size wings carefully, prefer defined risk.",
        cold_means="Skew calm — structures are freer.",
        horizons=(5, 20),
        page_route="/research/vol-regime?view=skew",
        scan_flag="atm_slope",
        decay_lens="skew",
        similar_lens="term_slope",
        hit_rule="mean_revert",
        notes=(
            "Scan flags the signed slope on a normalised 0-100 score; the page verdict "
            "reads the absolute slope as severity. similar-regime calls this lens term_slope. "
            "Decay trigger is contrarian on the sign: call-skew extreme (slope <= -0.25) is the "
            "hot side and expects the price down, put-skew extreme (slope >= +0.25) is the cold "
            "side and expects it up."
        ),
    ),
    LensSpec(
        id="term_slope",
        label="Term structure slope",
        kind="signed",
        source_table="features.option_surface_fit_daily",
        value_column="atm_vol",
        unit="far_minus_near_vol",
        bands=Bands(),
        hot_means="Backwardation — near-dated vol above far-dated; event or stress priced.",
        cold_means="Steep contango — calendar sellers are paid to wait.",
        horizons=(5, 20),
        page_route="/research/vol-regime?view=skew",
        notes="Bands land with the term-structure verdict (Phase C2).",
    ),
    LensSpec(
        id="opex_pin",
        label="OpEx pin distance",
        kind="distance",
        source_table="features.option_metric_max_pain_daily",
        value_column="pin_pct_distance",
        unit="fraction_of_spot",
        bands=Bands(hot=0.010),
        hot_means="Spot within 1% of max pain into OpEx — pin / mean-revert hypothesis.",
        cold_means="",
        horizons=(5, 20),
        page_route="/research/dealer-levels?view=opex",
        scan_flag="pin",
        decay_lens="opex_pin",
        similar_lens="pin_distance",
        hit_rule="mean_revert",
        notes="Wave J widened the hot band from 0.5% to 1% (21 rows / 179d was too sparse).",
    ),
    LensSpec(
        id="gex_regime",
        label="Gamma regime",
        kind="categorical",
        source_table="features.option_metric_gex_levels_daily",
        value_column="total_net_gex",
        unit="positive|negative",
        bands=Bands(),
        hot_means="Negative net gamma — dealers chase moves; realised vol expands.",
        cold_means="Positive net gamma — dealers damp moves; realised vol compresses.",
        horizons=(5, 20),
        page_route="/research/dealer-levels?view=gex",
        decay_lens="gex_regime",
        similar_lens="gex_notional",
        hit_rule="magnitude",
        move_threshold=(0.02, 0.04),
        notes=(
            "Sign of total_net_gex at the ~30 DTE expiry plus spot vs zero-gamma. Decay hit is "
            "a magnitude: negative gamma (hot) expects a 5d move of at least 2% (20d: 4%), "
            "positive gamma (cold) expects a smaller one."
        ),
        categories=(("negative", "hot"), ("positive", "cold")),
    ),
    LensSpec(
        id="terrain_regime",
        label="Terrain regime",
        kind="categorical",
        source_table="features.stock_forecast_terrain_daily",
        value_column="regime",
        unit="range|trending|crash-risk",
        bands=Bands(),
        hot_means="crash-risk — do not add risk.",
        cold_means="range — fade extremes.",
        horizons=(5, 20),
        page_route="/research/scenario?view=model",
        scan_flag="terrain",
        decay_lens="terrain_regime",
        similar_lens="regime",
        hit_rule="mean_revert",
        notes=(
            "Scan's terrain flag is the pin_score band, not the regime label. Decay trigger: "
            "only crash-risk fires (hot, expects the price down); trigger_value carries tail_risk."
        ),
        categories=(("crash-risk", "hot"), ("trending", "lean_hot"), ("range", "neutral")),
    ),
    LensSpec(
        id="momentum",
        label="Momentum score",
        kind="score",
        source_table="features.stock_signal_momentum_daily",
        value_column="score",
        unit="score_0_100",
        bands=SCORE_BANDS,
        hot_means="Grade A/A+ path EXT — momentum wants to release.",
        cold_means="HALT — momentum exhausted.",
        horizons=(5, 20),
        page_route="/research/momentum-radar",
    ),
    LensSpec(
        id="sepa",
        label="SEPA composite",
        kind="score",
        source_table="features.stock_signal_sepa_daily",
        value_column="sepa_score",
        unit="score_0_100",
        bands=SCORE_BANDS,
        hot_means="Stage 2 SETUP / PIVOT with grade A — the Loop's stock universe.",
        cold_means="Stage 4 decline — avoid.",
        horizons=(5, 20),
        page_route="/research/explorer",
        notes="The Loop policy applies its own min_score (default 70) on top of these bands.",
    ),
    LensSpec(
        id="order_sentiment",
        label="Order-flow sentiment",
        kind="signed",
        source_table="features.option_flow_sentiment_daily",
        value_column="sentiment_score",
        unit="score_-100_100",
        bands=Bands(hot=30.0, cold=-30.0),
        hot_means="Tape leans long — follow flow, confirm with GEX walls.",
        cold_means="Tape leans short — confirm with the put wall / zero gamma.",
        horizons=(1, 5),
        page_route="/research/flow",
        decay_lens="order_sentiment",
        data_dependency="option_trades_tape",
        hit_rule="follow",
        notes=(
            "Without the trades tape the score is an OI proxy and carries no verdict; the decay "
            "builder only writes triggers from tape-sourced rows, so the record stays empty until "
            "the tape exists."
        ),
    ),
    LensSpec(
        id="forecast_path",
        label="Forecast path hit",
        kind="categorical",
        source_table="features.stock_backtest_settlement",
        value_column="path_hit",
        unit="hit|miss",
        bands=Bands(),
        hot_means="Path call settled as a hit.",
        cold_means="Path call missed.",
        horizons=(1,),
        page_route="/research/scenario?view=sessions",
        notes="Read through /research/forecast/hit-rate and regime-stats, not classified here.",
    ),
)

LENSES: dict[str, LensSpec] = {spec.id: spec for spec in _SPECS}


def _spec(lens_id: str) -> LensSpec:
    try:
        return LENSES[lens_id]
    except KeyError as exc:
        raise ValueError(f"unknown lens: {lens_id!r}") from exc


def band_for_score(value: float | None) -> Band | None:
    """The shared 0–100 bands. 40–60 inclusive is neutral, as Scan always had it."""
    if value is None:
        return None
    v = float(value)
    if v >= SCORE_HOT:
        return "hot"
    if v <= SCORE_COLD:
        return "cold"
    if SCORE_LEAN_COLD <= v <= SCORE_LEAN_HOT:
        return "neutral"
    return "lean_hot" if v > SCORE_LEAN_HOT else "lean_cold"


def classify(lens_id: str, value: float | None, *, fractions_as_pct: bool = False) -> Band | None:
    """Band for a reading on the lens' own scale.

    ``fractions_as_pct`` reads a value in [0, 1] as a percent — what Signal Decay
    has always done for percentile inputs, and what Scan (whose inputs are
    already 0–100) must not do.
    """
    spec = _spec(lens_id)
    if value is None or spec.kind == "categorical":
        return None
    v = float(value)
    if spec.kind == "score":
        if fractions_as_pct and 0.0 <= v <= 1.0:
            v *= 100.0
        return band_for_score(v)
    b = spec.bands
    if spec.kind == "signed":
        if b.hot is not None and v >= b.hot:
            return "hot"
        if b.cold is not None and v <= b.cold:
            return "cold"
        return "neutral" if (b.hot is not None or b.cold is not None) else None
    if spec.kind == "severity":
        a = abs(v)
        if b.hot is not None and a >= b.hot:
            return "hot"
        if b.lean_hot is not None and a >= b.lean_hot:
            return "lean_hot"
        return "neutral"
    # distance: near the magnet is the only signal.
    if b.hot is not None and abs(v) <= b.hot:
        return "hot"
    return "neutral"


def classify_category(lens_id: str, category: str | None) -> Band | None:
    """Band for a categorical reading (terrain regime, gamma regime); None when unmapped."""
    spec = _spec(lens_id)
    if category is None:
        return None
    key = str(category).strip().lower()
    for value, band in spec.categories:
        if value == key:
            return band  # type: ignore[return-value]
    return None


def trigger_side(lens_id: str, value: float | None, *, fractions_as_pct: bool = False) -> str | None:
    """Signal Decay's trigger side: only hot and cold count as triggers."""
    band = classify(lens_id, value, fractions_as_pct=fractions_as_pct)
    if band in ("hot", "cold"):
        return band
    return None


def scan_flag(band: Band | None) -> str | None:
    """Scan's sparse flag: hot / cold / neutral, and nothing for the lean bands."""
    if band in ("hot", "cold", "neutral"):
        return band
    return None


def decay_lens_ids() -> tuple[str, ...]:
    """Lens values that exist in stock_signal_lens_hit_daily today."""
    return tuple(spec.decay_lens for spec in _SPECS if spec.decay_lens)


def similar_lens_ids() -> tuple[str, ...]:
    """Lens params /research/similar-regime accepts."""
    return tuple(spec.similar_lens for spec in _SPECS if spec.similar_lens)


def public_registry() -> list[dict[str, Any]]:
    """The registry as the API and MCP hand it out."""
    out: list[dict[str, Any]] = []
    for spec in _SPECS:
        d = asdict(spec)
        d["horizons"] = list(spec.horizons)
        d["categories"] = dict(spec.categories)
        out.append(d)
    return out


def score_bands() -> dict[str, float]:
    return {
        "hot": SCORE_HOT,
        "lean_hot": SCORE_LEAN_HOT,
        "lean_cold": SCORE_LEAN_COLD,
        "cold": SCORE_COLD,
    }
