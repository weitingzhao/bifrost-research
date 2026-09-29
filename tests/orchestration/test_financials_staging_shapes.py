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

Capex exists only in the v1 shape, so ``capex`` and ``free_cash_flow`` on
stg_cash_flow are v1 reads with no legacy fallback: NULL on a legacy row rather
than a figure invented from investing cash flow.
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
    # A model's line comments may name columns too; only the SQL counts.
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"\s+", " ", sql)


def _column(sql: str, name: str) -> str:
    """The expression that produces one output column of the final select."""
    targets = sql.split(" from raw_market.")[0]
    match = re.search(rf"(coalesce\( .*?\)) as {name}\b", targets)
    assert match, f"no coalesce(...) as {name}"
    # The shortest coalesce ending right before this alias.
    expr = match.group(1)
    return expr[expr.rfind("coalesce( ") :]


def _targets(model: str) -> dict[str, str]:
    """Output column -> expression, in the final select's order."""
    sql = _rendered(model)
    match = re.search(r"\bselect (.*) from raw_market\.", sql)
    assert match, model
    body, targets, depth, start = match.group(1), [], 0, 0
    for i, char in enumerate(body):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            targets.append(body[start:i].strip())
            start = i + 1
    targets.append(body[start:].strip())
    out: dict[str, str] = {}
    for target in targets:
        aliased = re.fullmatch(r"(.+) as ([a-z0-9_]+)", target)
        name, expr = (aliased.group(2), aliased.group(1)) if aliased else (target, target)
        out[name] = expr
    return out


def _v1(name: str) -> str:
    """The guarded v1 read, as financial_value / financial_v1_value render it."""
    return f"case when jsonb_typeof(data -> '{name}') = 'number' then (data ->> '{name}')::numeric end"


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


def test_capex_and_free_cash_flow_are_v1_reads_only() -> None:
    targets = _targets("stg_cash_flow")
    capex, ocf = "purchase_of_property_plant_and_equipment", "net_cash_from_operating_activities"
    assert targets["capex"] == _v1(capex)
    # The vendor signs capex as a cash flow, negative when cash is spent, so free
    # cash flow adds it. A minus (or an abs()) would count spending as income.
    assert targets["free_cash_flow"] == f"{_v1(ocf)} + {_v1(capex)}"
    for column in ("capex", "free_cash_flow"):
        # No legacy fallback: that shape has no capex line, and investing cash
        # flow is not a stand-in for one.
        assert "->> 'value'" not in targets[column], column
        assert "investing" not in targets[column], column


def test_cash_flow_columns_keep_their_order() -> None:
    # The new columns come last, so every column before them keeps its place.
    assert list(_targets("stg_cash_flow")) == [
        "symbol",
        "period_date",
        "period_type",
        "fiscal_year",
        "fiscal_quarter",
        "fetched_at",
        "operating_cf",
        "investing_cf",
        "financing_cf",
        "net_cash_flow",
        "capex",
        "free_cash_flow",
    ]


@pytest.mark.parametrize("model", sorted(READS))
def test_every_documented_column_is_produced(model: str) -> None:
    # _staging__models.yml documented stg_cash_flow.free_cash_flow for a long
    # time before the model produced it.
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((DBT / "models" / "staging" / "_staging__models.yml").read_text())
    (entry,) = [m for m in doc["models"] if m["name"] == model]
    documented = {column["name"] for column in entry.get("columns", [])}
    missing = documented - set(_targets(model))
    assert not missing, (model, sorted(missing))
