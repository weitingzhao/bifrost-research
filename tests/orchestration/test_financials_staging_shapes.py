"""The financials staging models read a raw_market row in either shape.

The plugin moved from the vendor's legacy /vX/reference/financials (XBRL, one
object per concept: ``revenues: {value, unit, label}``) to its v1 statements
(flat numbers: ``revenue: 123.0``) ahead of the legacy endpoint's 2026-10-09
sunset. Symbols the v1 source does not cover keep their legacy rows, so every
figure is read both ways, v1 first.

Some names exist in both shapes (``basic_earnings_per_share``,
``gross_profit``, ``cost_of_revenue``). Casting a legacy object to numeric
raises, and one raising row fails the whole dbt build and every SEPA mart
behind it, so a v1 read is only taken when the value is a number.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")

DBT = Path(__file__).resolve().parents[2] / "src" / "bifrost_research" / "dbt"

# model -> {output column: (v1 name, legacy name)}. Derived columns
# (gross_profit, noncurrent_liabilities) are checked separately below.
READS = {
    "stg_income_stmt": {
        "revenue": ("revenue", "revenues"),
        "eps": ("basic_earnings_per_share", "basic_earnings_per_share"),
        "net_income": ("consolidated_net_income_loss", "net_income_loss"),
        "operating_income": ("operating_income", "operating_income_loss"),
        "cost_of_revenue": ("cost_of_revenue", "costs_and_expenses"),
        "operating_expenses": ("total_operating_expenses", "operating_expenses"),
    },
    "stg_balance_sheet": {
        "total_assets": ("total_assets", "assets"),
        "total_liabilities": ("total_liabilities", "liabilities"),
        "total_equity": ("total_equity", "equity"),
        "current_assets": ("total_current_assets", "current_assets"),
        "current_liabilities": ("total_current_liabilities", "current_liabilities"),
        "fixed_assets": ("property_plant_equipment_net", "fixed_assets"),
        "equity_to_parent": ("total_equity_attributable_to_parent", "equity_attributable_to_parent"),
    },
    "stg_cash_flow": {
        "operating_cf": ("net_cash_from_operating_activities", "net_cash_flow_from_operating_activities"),
        "investing_cf": ("net_cash_from_investing_activities", "net_cash_flow_from_investing_activities"),
        "financing_cf": ("net_cash_from_financing_activities", "net_cash_flow_from_financing_activities"),
        "net_cash_flow": ("change_in_cash_and_equivalents", "net_cash_flow"),
    },
}

_V1_READ = re.compile(r"\(data ->> '([a-z0-9_]+)'\)::numeric")


def _rendered(model: str) -> str:
    macro = (DBT / "macros" / "financial_value.sql").read_text()
    body = (DBT / "models" / "staging" / f"{model}.sql").read_text()
    template = jinja2.Environment().from_string(macro + "\n" + body)
    sql = template.render(config=lambda **_: "", source=lambda _s, table: f"raw_market.{table}")
    return re.sub(r"\s+", " ", sql)


def _column(sql: str, name: str) -> str:
    """The expression that produces one output column of the final select."""
    targets = sql.split(" from raw_market.")[0]
    match = re.search(rf"(coalesce\( .*?\)) as {name}\b", targets)
    assert match, f"no coalesce(...) as {name}"
    # The shortest coalesce ending right before this alias.
    expr = match.group(1)
    return expr[expr.rfind("coalesce( ") :]


@pytest.mark.parametrize("model", sorted(READS))
def test_every_figure_is_read_in_both_shapes(model: str) -> None:
    sql = _rendered(model)
    for column, (v1, legacy) in READS[model].items():
        expr = _column(sql, column)
        assert f"jsonb_typeof(data -> '{v1}') = 'number'" in expr, (model, column)
        assert f"(data ->> '{v1}')::numeric" in expr, (model, column)
        assert f"(data -> '{legacy}' ->> 'value')::numeric" in expr, (model, column)
        # v1 first: a legacy fallback must not shadow a v1 number.
        assert expr.index(f"'{v1}'") < expr.index(f"'{legacy}' ->> 'value'"), (model, column)


@pytest.mark.parametrize("model", sorted(READS))
def test_no_v1_read_goes_unguarded(model: str) -> None:
    sql = _rendered(model)
    reads = _V1_READ.findall(sql)
    assert reads
    for name in reads:
        assert f"jsonb_typeof(data -> '{name}') = 'number'" in sql, (model, name)


def test_the_derived_figures_keep_their_legacy_arithmetic() -> None:
    income = _rendered("stg_income_stmt")
    gross = _column(income, "gross_profit")
    assert "jsonb_typeof(data -> 'gross_profit') = 'number'" in gross
    assert (
        "(data -> 'revenues' ->> 'value')::numeric "
        "- coalesce((data -> 'costs_and_expenses' ->> 'value')::numeric, 0)"
    ) in gross

    balance = _rendered("stg_balance_sheet")
    noncurrent = _column(balance, "noncurrent_liabilities")
    assert "(data ->> 'total_liabilities')::numeric - (data ->> 'total_current_liabilities')::numeric" in noncurrent
    assert "(data -> 'noncurrent_liabilities' ->> 'value')::numeric" in noncurrent
