"""No dbt view may ref() a table model.

dbt-postgres rebuilds a table by renaming the old relation to ``__dbt_backup`` and
then ``drop table ... cascade``. A view is bound to its upstream by OID, so it
follows the rename onto the backup and the CASCADE drops it. The view is gone
from that moment until dbt reaches it again later in the run -- and if the run
dies in between, it stays gone until the next successful run.

That is exactly how the Stock Screener went down on 2026-09-11:
``mart_sepa_criteria_stats`` (a view) was dropped at 02:32:42 when
``mart_sepa_fundamental_eval`` rebuilt, and the run was OOMKilled at 02:34:31.

Views over ``source()`` tables are fine -- dbt never rebuilds those.
"""

from __future__ import annotations

import re
from pathlib import Path

MODELS = Path(__file__).resolve().parents[2] / "src" / "bifrost_research" / "dbt" / "models"

_CONFIG_VIEW = re.compile(r"materialized\s*=\s*['\"]view['\"]")
_REF = re.compile(r"ref\(\s*['\"]([a-z0-9_]+)['\"]\s*\)")


def _materializations() -> dict[str, str]:
    # Every folder in dbt_project.yml defaults to table; only an explicit
    # config(materialized='view') makes a view.
    out: dict[str, str] = {}
    for sql in MODELS.rglob("*.sql"):
        text = sql.read_text()
        out[sql.stem] = "view" if _CONFIG_VIEW.search(text) else "table"
    return out


def test_models_directory_is_where_this_test_thinks() -> None:
    assert (MODELS / "marts" / "mart_sepa_criteria_stats.sql").is_file()


def test_no_view_refs_a_table_model() -> None:
    kinds = _materializations()
    offenders: list[str] = []
    for sql in MODELS.rglob("*.sql"):
        if kinds[sql.stem] != "view":
            continue
        for upstream in sorted(set(_REF.findall(sql.read_text()))):
            if kinds.get(upstream) == "table":
                offenders.append(f"{sql.stem} (view) -> ref('{upstream}') (table)")
    assert not offenders, (
        "a view over a table model is dropped by that table's rebuild CASCADE:\n  "
        + "\n  ".join(offenders)
    )


def test_criteria_stats_is_a_table() -> None:
    assert _materializations()["mart_sepa_criteria_stats"] == "table"
