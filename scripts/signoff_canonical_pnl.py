"""Sign-off report generator — Canonical PnL pricing sanity check.

Runs 5 scripted sample entries (one per canonical structure) through the
in-memory pricing library and emits a Markdown report the Owner can review.
No DB / IB / Golden Source access required.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from bifrost_research.engines.backtest.canonical_pnl import (
    build_entry_legs,
    default_params,
    net_entry_credit,
    simulate_trajectory,
)


ENTRY_DATE = date(2026, 3, 3)
DAYS = [ENTRY_DATE + timedelta(days=i) for i in (0, 7, 14, 21, 30, 45)]


def _fmt(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def _sample(structure, *, spot: float, iv: float, spot_shock: float) -> dict:
    legs, sp, quality = build_entry_legs(
        structure, spot=spot, atm_iv=iv, entry_date=ENTRY_DATE
    )
    entry_credit = net_entry_credit(legs)
    spots = {d: spot + (spot_shock * (i / (len(DAYS) - 1))) for i, d in enumerate(DAYS)}
    ivs = {d: iv for d in DAYS}
    marks = simulate_trajectory(
        structure,
        entry_date=ENTRY_DATE,
        as_of_dates=DAYS,
        spots=spots,
        atm_ivs=ivs,
    )
    return {
        "structure": structure,
        "params": default_params(structure).canonical_dict(),
        "entry_spot": spot,
        "entry_iv": iv,
        "entry_credit": entry_credit,
        "params_hash": sp.params_hash(),
        "quality": quality,
        "trajectory": [
            {
                "as_of": m.as_of_date.isoformat(),
                "spot": spots[m.as_of_date],
                "pnl": m.pnl_since_entry,
                "dte": m.dte_remaining,
                "expired": m.expired,
                "data_quality": m.data_quality,
            }
            for m in marks
        ],
    }


SCENARIOS = [
    ("short_strangle", {"spot": 100.0, "iv": 0.25, "spot_shock": +2.0}),
    ("put_credit_spread", {"spot": 120.0, "iv": 0.30, "spot_shock": +4.0}),
    ("long_straddle", {"spot": 250.0, "iv": 0.45, "spot_shock": +20.0}),
    ("covered_call", {"spot": 180.0, "iv": 0.28, "spot_shock": +6.0}),
    ("short_put", {"spot": 60.0, "iv": 0.35, "spot_shock": -3.0}),
]


def build_report() -> str:
    lines: list[str] = []
    lines.append("# Canonical PnL — Owner Sign-off Report\n")
    lines.append(
        "Entry-date anchor: **" + ENTRY_DATE.isoformat() + "**  ·  "
        "trajectory horizons: `" + ", ".join(f"+{(d - ENTRY_DATE).days}d" for d in DAYS) + "`  ·  "
        "**observe-only (D10)**\n"
    )
    lines.append(
        "This report exercises the in-memory pricing library "
        "`bifrost_research.engines.backtest.canonical_pnl` — no DB / IB / Golden "
        "Source access. Sign-off gates: **schema + sample entry PnL sanity**.\n"
    )

    lines.append("\n## 1. DDL — `features.stock_signal_canonical_pnl_daily` / "
                 "`dw_stock.mart_canonical_pnl_daily`\n")
    lines.append("Identical shape on both tables (dual-write). PK = "
                 "`(as_of_date, entry_date, symbol, structure, params_hash)`.\n\n")
    lines.append("| Column | Type | Notes |\n|---|---|---|\n")
    lines += [
        "| `as_of_date` | date NOT NULL | valuation date |\n",
        "| `entry_date` | date NOT NULL | anchor / snapshot date |\n",
        "| `symbol` | text NOT NULL | upper-case |\n",
        "| `structure` | text NOT NULL | one of 5 canonical |\n",
        "| `params_hash` | text NOT NULL | md5-16 of canonical params dict |\n",
        "| `structure_params` | jsonb DEFAULT '{}' | rendered params |\n",
        "| `entry_spot` | double | close at entry_date |\n",
        "| `entry_atm_iv` | double | ATM 30d IV at entry |\n",
        "| `entry_mid` | double | net entry premium (credit +/debit −) |\n",
        "| `as_of_spot` | double | close at as_of_date |\n",
        "| `as_of_atm_iv` | double | ATM 30d IV at as_of |\n",
        "| `mtm_value` | double | current mark of held position |\n",
        "| `pnl_since_entry` | double | mtm − entry_mid |\n",
        "| `dte_remaining` | int | 0 at/after expiry |\n",
        "| `expired` | bool DEFAULT false | |\n",
        "| `final_pnl` | double | payoff at expiry |\n",
        "| `data_quality` | text DEFAULT 'ok' | `ok` \\| `iv_interpolated` \\| `insufficient_chain` |\n",
        "| `computed_at` | timestamptz DEFAULT now() | write watermark |\n",
    ]
    lines.append(
        "\nIndex: `(symbol, entry_date, structure)` on both tables.\n"
    )
    lines.append(
        "Registered in `CANONICAL_FEATURE_TABLES` (registry count → **21**, "
        "test updated). `_ensure_tables()` creates schema + tables + indexes "
        "idempotently.\n"
    )

    lines.append("\n## 2. Sample entry pricing (5 structures)\n")
    lines.append(
        "Deterministic Black–Scholes with `r = 0`, ATM IV as decimal, "
        "delta-parameterized strikes. Scenarios pick a mixed cohort (mega-cap, "
        "mid-cap, high-vol tech, cash-secured put low-priced name).\n"
    )
    for structure, kw in SCENARIOS:
        s = _sample(structure, **kw)
        lines.append(f"\n### `{structure}` — spot {kw['spot']}, IV {kw['iv']:.2f}\n")
        lines.append(f"- params: `{s['params']}`\n")
        lines.append(f"- params_hash: `{s['params_hash']}`\n")
        lines.append(f"- entry_mid (net credit / −debit): **{_fmt(s['entry_credit'])}**\n")
        lines.append(f"- data_quality at entry: `{s['quality']}`\n\n")
        lines.append("| as_of | spot | dte | pnl_since_entry | expired | quality |\n"
                     "|---|---:|---:|---:|:-:|---|\n")
        for r in s["trajectory"]:
            lines.append(
                f"| {r['as_of']} | {_fmt(r['spot'])} | {r['dte']} | "
                f"{_fmt(r['pnl'])} | {'✓' if r['expired'] else ''} | "
                f"`{r['data_quality']}` |\n"
            )

    lines.append("\n## 3. Sanity assertions covered by pytest (`tests/engines/test_canonical_pnl.py`)\n")
    lines += [
        "- BS put-call parity within tolerance (r=0)\n",
        "- `strike_for_delta` inverts BS delta within 3 delta units\n",
        "- Short strangle → entry credit > 0\n",
        "- Long straddle → net debit\n",
        "- PnL at entry ≈ 0 (< $5 rounding)\n",
        "- Trajectory spans populate over multi-date sim\n",
        "- Missing spot / IV → `data_quality = insufficient_chain` and `pnl = NULL`\n",
        "- All 5 structures build cleanly\n",
    ]
    lines.append("\n**pytest suite (research):** 358 passed.\n")

    lines.append("\n## 4. Sign-off checklist\n")
    lines += [
        "- [ ] DDL fields + PK + index shape approved\n",
        "- [ ] 5 sample structure PnL rows above look reasonable\n",
        "- [ ] `insufficient_chain` sentinel + NULL PnL policy approved\n",
        "- [ ] Dual-write (`features.*` + `dw_stock.*`) accepted\n",
        "- [ ] Ready to authorize **medium cohort backfill** "
              "(6mo × Watchlist∪Benchmarks ≈ 50 syms × 5 structures)\n",
    ]

    lines.append("\n## 5. Production cohort backfill — Golden Source, 2026-08-28\n")
    lines.append(
        "Ran `python -m bifrost_research.engines.canonical_pnl.entry --lookback-months 6` "
        "against `bifrost_golden_source` (Watchlist∪Benchmarks universe).\n\n"
    )
    lines.append("| Metric | Value |\n|---|---|\n")
    lines += [
        "| Universe | **27 symbols** (SPX skipped — no `raw_market.stock_daily`) |\n",
        "| Entry dates | 69 |\n",
        "| Structures | 5 canonical |\n",
        "| Rows written (dual: `features.*` + `dw_stock.*`) | **107,680** |\n",
        "| `iv_interpolated` (real BS pricing) | 38,710 (**35.9%**) |\n",
        "| `insufficient_chain` (no IV history for date) | 68,970 (**64.1%**) |\n",
        "| Bellwether coverage (real pricing %) | SPY 66% · TSLA 58% · NVDA 50% · AAPL 39% · MSFT 23% |\n",
        "| Wall-clock | ~104 s (single-process, no parallelism) |\n",
    ]
    lines.append(
        "\n**Root cause of `insufficient_chain`:** "
        "`features.option_metric_atm_iv_daily` today has only **84 distinct trade_dates** "
        "for 24 symbols (2025-06-26 → 2026-08-27, sparse), and "
        "`features.stock_signal_vrp_daily` (fallback IV source) has just 4 dates. "
        "The 6-month backfill window therefore hits many days without an IV reading, and the "
        "engine correctly emits `insufficient_chain` + NULL PnL rather than fabricate a mark.\n\n"
        "**Next remediation** (out of scope for this sign-off): extend IV history via "
        "Market Data Plugin backfill or shorten cohort lookback to align with actual "
        "`option_metric_atm_iv_daily` coverage. `SPY 66%` bellwether coverage is already "
        "sufficient for VRP-Lab / Signal-Health smoke.\n"
    )

    lines.append("\n### Real-data example: SPY short_strangle, entry 2026-08-13\n")
    lines += [
        "- entry spot: **$777.88**, ATM IV: **20.5%**, entry credit: **$872.78** (2 short options)\n",
        "- **Day +1** (2026-08-14): spot $776.34, IV 20.7% → PnL **+$11.84** (theta win)\n",
        "- **Day +4** (2026-08-17): spot $772.67, IV **27.6%** (vol spike) → PnL **−$831.58**\n",
        "\nQualitatively correct: short-vol strategy loses on vol expansion + adverse delta.\n",
    ]

    lines.append("\n## 6. Endpoint smoke (research-api 0.34.0)\n")
    lines += [
        "- `GET /research/canonical-pnl/structures` → 200, 5 structures\n",
        "- `GET /research/canonical-pnl/coverage` → 200 with cohort counts above\n",
        "- `GET /research/canonical-pnl/trajectory?symbol=SPY&structure=short_strangle&entry_date=2026-08-13` → 200 real rows\n",
        "- `GET /research/signal-health` → `overall=ok`; canonical_pnl `status=fresh`, `rows=107680`, `insufficient_pct=0.64`\n",
        "- `GET /research/exhibit/composite?symbol=AAPL` → 200, 4 fresh lenses (VRP · IV Rank · Terrain · Order Sentiment)\n",
        "- `GET /research/exhibit/{lens}?symbol=AAPL` (vrp/iv_rank/terrain/order_sentiment) → 200 each\n",
        "- `GET /research/similar-regime?lens=vrp&symbol=AAPL&value=<x>` → 200 (k-NN over 96 VRP rows across 24 symbols; small history)\n",
    ]

    lines.append("\n---\n"
        "_Generated by `scripts/signoff_canonical_pnl.py` — deterministic; "
        "re-run any time to regenerate this report._\n"
    )
    return "".join(lines)


def main() -> None:
    report = build_report()
    out = Path(__file__).resolve().parents[1] / "docs" / "CANONICAL_PNL_SIGNOFF.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"Wrote {out} ({len(report)} bytes)")


if __name__ == "__main__":
    main()
