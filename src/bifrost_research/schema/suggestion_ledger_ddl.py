"""Suggestion ledger — append-only (stage 2, design 2026-10-05, Owner-approved).

Three tables in Golden Source ``research``:

- ``suggestion``: one complete, settleable suggestion per row — the legs, the
  management rules, the expectation and a frozen snapshot of the inputs. Never
  updated; a change of mind is a new row with ``supersedes_id``.
- ``suggestion_settlement``: one row per (suggestion, basis, method_version).
  A method upgrade writes a new version beside the old one.
- ``suggestion_adoption``: adoption events (adopted / declined / implicit_match).

Append-only is enforced twice: UPDATE / DELETE / TRUNCATE are revoked from
``bifrost``, and a trigger refuses UPDATE / DELETE / TRUNCATE for everyone short
of disabling it. The owner (``analytics_writer``) keeps UPDATE: foreign-key
checks lock the referenced row FOR KEY SHARE as the owner, which needs it —
revoking it makes every settlement and adoption insert fail.

D10 BLOCKED — a suggestion is a record; nothing here reaches an order.
"""

from __future__ import annotations

from typing import Any

from bifrost_research.schema.schemas import SCHEMA_RESEARCH

LEDGER_TABLES = ("suggestion", "suggestion_settlement", "suggestion_adoption")

SUGGESTION_SOURCES = ("baseline", "simulator", "copilot", "lens", "pine", "manual")
SUGGESTION_KINDS = ("option_structure", "stock_position", "stand_aside")
# symbol_paired (S4 control, Owner 2026-10-06): the same structure on the same
# name, entered on a session near the signal when the source did not fire. A
# table created before it is widened once by schema/migrate_symbol_paired.py;
# this module stays DROP-free.
SETTLEMENT_BASES = ("model", "model_stress", "baseline_paired", "actual", "symbol_paired")
SETTLEMENT_STATUSES = ("settled", "void")
ADOPTION_EVENTS = ("adopted", "declined", "implicit_match")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def ledger_statements() -> list[str]:
    """Every statement, in order — also what the DDL plan shows the Owner."""
    s = SCHEMA_RESEARCH
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {s}.suggestion (
            suggestion_id     text        PRIMARY KEY,
            issue_key         text        NOT NULL UNIQUE,
            issued_at         timestamptz NOT NULL DEFAULT now(),
            as_of_session     date        NOT NULL,
            source            text        NOT NULL CHECK (source IN ({_in(SUGGESTION_SOURCES)})),
            source_ref        text        NOT NULL,
            source_version    text        NOT NULL,
            symbol            text        NOT NULL,
            kind              text        NOT NULL CHECK (kind IN ({_in(SUGGESTION_KINDS)})),
            structure         text,
            legs_json         jsonb       NOT NULL DEFAULT '[]'::jsonb,
            take_profit_pct   numeric,
            stop_loss_mult    numeric,
            exit_dte          integer,
            max_hold_days     integer,
            horizon_days      integer,
            expected_credit   numeric,
            max_loss          numeric,
            pop               numeric     CHECK (pop IS NULL OR (pop >= 0 AND pop <= 1)),
            expected_pnl      numeric,
            conviction        smallint    CHECK (conviction IS NULL OR conviction BETWEEN 1 AND 5),
            snapshot_json     jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            inputs_hash       text        NOT NULL,
            rationale         text,
            hypothesis_id     text        REFERENCES {s}.hypothesis(id),
            candidate_id      text        REFERENCES {s}.candidate_pool(id),
            supersedes_id     text        REFERENCES {s}.suggestion(suggestion_id),
            CHECK (kind <> 'option_structure'
                   OR (structure IS NOT NULL AND jsonb_array_length(legs_json) > 0))
        )
        """,
        f"CREATE INDEX IF NOT EXISTS suggestion_session ON {s}.suggestion (as_of_session DESC)",
        f"CREATE INDEX IF NOT EXISTS suggestion_source_session ON {s}.suggestion (source, as_of_session DESC)",
        f"CREATE INDEX IF NOT EXISTS suggestion_symbol_session ON {s}.suggestion (symbol, as_of_session DESC)",
        f"""
        CREATE TABLE IF NOT EXISTS {s}.suggestion_settlement (
            suggestion_settlement_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            suggestion_id            text        NOT NULL REFERENCES {s}.suggestion(suggestion_id),
            basis                    text        NOT NULL CHECK (basis IN ({_in(SETTLEMENT_BASES)})),
            method_version           text        NOT NULL,
            status                   text        NOT NULL CHECK (status IN ({_in(SETTLEMENT_STATUSES)})),
            void_reason              text,
            entry_date               date,
            exit_date                date,
            exit_reason              text,
            days_held                integer,
            entry_credit             numeric,
            gross_pnl                numeric,
            slippage_cost            numeric,
            commission               numeric,
            net_pnl                  numeric,
            max_loss                 numeric,
            margin                   numeric,
            risk_basis               numeric,
            return_on_risk           numeric,
            mfe                      numeric,
            mae                      numeric,
            fill_basis               text,
            detail_json              jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            settled_at               timestamptz NOT NULL DEFAULT now(),
            UNIQUE (suggestion_id, basis, method_version),
            CHECK (status <> 'void' OR void_reason IS NOT NULL)
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS suggestion_settlement_exit
        ON {s}.suggestion_settlement (basis, exit_date DESC)
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.suggestion_adoption (
            suggestion_adoption_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            suggestion_id          text        NOT NULL REFERENCES {s}.suggestion(suggestion_id),
            event                  text        NOT NULL CHECK (event IN ({_in(ADOPTION_EVENTS)})),
            occurred_at            timestamptz NOT NULL DEFAULT now(),
            actor                  text,
            strategy_plan_id       text,
            trade_id               text,
            match_json             jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
            note                   text
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS suggestion_adoption_suggestion
        ON {s}.suggestion_adoption (suggestion_id, occurred_at)
        """,
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS suggestion_adoption_one_implicit
        ON {s}.suggestion_adoption (suggestion_id)
        WHERE event = 'implicit_match'
        """,
        f"""
        CREATE OR REPLACE FUNCTION {s}.suggestion_ledger_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '%.% is append-only (% refused)', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """,
        *[
            stmt
            for t in LEDGER_TABLES
            for stmt in (
                f"""
                CREATE OR REPLACE TRIGGER {t}_append_only_row
                BEFORE UPDATE OR DELETE ON {s}.{t}
                FOR EACH ROW EXECUTE FUNCTION {s}.suggestion_ledger_append_only()
                """,
                f"""
                CREATE OR REPLACE TRIGGER {t}_append_only_truncate
                BEFORE TRUNCATE ON {s}.{t}
                FOR EACH STATEMENT EXECUTE FUNCTION {s}.suggestion_ledger_append_only()
                """,
            )
        ],
    ]


def revoke_statements() -> list[str]:
    """Best-effort: the role may not exist in a dev database. Not the owner — see the module doc."""
    s = SCHEMA_RESEARCH
    return [f"REVOKE UPDATE, DELETE, TRUNCATE ON {s}.{t} FROM bifrost" for t in LEDGER_TABLES]


def create_suggestion_ledger(cur: Any) -> None:
    from bifrost_research.schema.ddl import _try_each

    for sql in ledger_statements():
        cur.execute(sql)
    _try_each(cur, revoke_statements())


__all__ = [
    "ADOPTION_EVENTS",
    "LEDGER_TABLES",
    "SETTLEMENT_BASES",
    "SETTLEMENT_STATUSES",
    "SUGGESTION_KINDS",
    "SUGGESTION_SOURCES",
    "create_suggestion_ledger",
    "ledger_statements",
    "revoke_statements",
]
