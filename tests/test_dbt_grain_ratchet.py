"""Guard: every dbt model is documented in a yml and carries a grain test (TD-111).

A grain test is a column-level ``unique`` or a model-level
``dbt_utils.unique_combination_of_columns``. int_stock_daily_enriched is
incremental on (symbol, trade_date) and had only not_null tests, so a broken
merge would have passed ``dbt build`` green; mart_sepa_tier_options was in no
yml at all. Grains measured clean on 2026-10-06 before the tests went in.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_DBT = Path(__file__).resolve().parents[1] / "src" / "bifrost_research" / "dbt"
_MODELS = _DBT / "models"
_GRAIN_TESTS = {"unique", "dbt_utils.unique_combination_of_columns"}


def _test_names(tests: list | None) -> set[str]:
    out: set[str] = set()
    for t in tests or []:
        out.add(t if isinstance(t, str) else next(iter(t)))
    return out


def _documented() -> dict[str, bool]:
    """model name -> has a grain test, over every yml under models/."""
    out: dict[str, bool] = {}
    for path in sorted(_MODELS.rglob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for model in doc.get("models") or []:
            names = _test_names(model.get("tests")) | _test_names(model.get("data_tests"))
            for col in model.get("columns") or []:
                names |= _test_names(col.get("tests")) | _test_names(col.get("data_tests"))
            out[model["name"]] = bool(names & _GRAIN_TESTS)
    return out


def test_every_model_is_in_a_yml() -> None:
    sql = {p.stem for p in _MODELS.rglob("*.sql")}
    missing = sorted(sql - set(_documented()))
    assert not missing, f"models with no yml entry: {missing}"


def test_every_model_has_a_grain_test() -> None:
    bare = sorted(name for name, has in _documented().items() if not has)
    assert not bare, f"models without a unique / unique_combination_of_columns test: {bare}"


def test_no_generic_test_sits_on_the_singular_path() -> None:
    """A ``{% test %}`` block under tests/ (not tests/generic/) is never applied (TD-111)."""
    stray = [
        p.relative_to(_DBT).as_posix()
        for p in (_DBT / "tests").rglob("*.sql")
        if "{% test " in p.read_text(encoding="utf-8") and "generic" not in p.relative_to(_DBT / "tests").parts
    ]
    assert not stray, f"generic test definitions outside tests/generic/: {stray}"
