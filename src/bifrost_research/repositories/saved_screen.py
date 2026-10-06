"""SQL layer for ``research.saved_screen`` — 6A saved-screen store.

A screen is one object with one id. The authoring face writes it, Trade's
result face renders the same object read-only, so the filter vocabulary is
defined here and nowhere else: ``definition`` is validated against the
condition catalog below, strictly — an unknown filter key or condition name
is an error, not a passthrough, because a definition Trade cannot render is
worse than a rejected save.

``vocabulary`` stamps which catalog the definition speaks. When the dbt
vocabulary changes, add a catalog under a new stamp; existing rows keep
theirs and stay renderable.

Two stamps are spoken:

* ``sepa_screener_wide.v1`` — the SEPA wide table's filters (search, path,
  grade, composite floor, trend and growth conditions). Method › Conditions
  still writes it.
* ``stock_screen.v2`` (0.181.0) — Stock screen's stages: per stage the
  conditions picked and, where the stage has one, its "at least N"; the Pine
  stage as one block (the ``pine:<script>:<side>`` picks, the window in
  sessions and Any / All); and the universe. Only conditions some store can
  evaluate are in the catalog — a condition the page draws as missing cannot
  be saved, because a screen that names it would cut nothing.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from bifrost_research.schema.schemas import TABLE_RESEARCH_PINE_SCRIPT, TABLE_RESEARCH_SAVED_SCREEN

VOCABULARY_V1 = "sepa_screener_wide.v1"

# mart_sepa_technical_eval / mart_sepa_fundamental_eval condition columns.
TECH_CONDITIONS_V1 = frozenset(
    {
        "price_gt_sma200",
        "sma150_gt_sma200",
        "price_gt_sma150",
        "sma50_gt_sma200",
        "avg_volume_50_gt_threshold",
        "close_ge_low52_x_1_3",
        "sma50_gt_sma150",
        "price_gt_sma50",
        "sma200_rising_1m",
        "close_ge_high52_x_0_75",
        "crs_ge_70",
    }
)
FUND_CONDITIONS_V1 = frozenset(
    {
        "eps_q2q_ge_25pct",
        "rev_q2q_ge_25pct",
        "eps_acc_2q",
        "rev_acc_2q",
        "eps_3y_ge_15pct",
        "rev_3y_ge_15pct",
        "eps_acc_fy",
        "rev_acc_fy",
    }
)
PATHS_V1 = frozenset({"PIVOT", "SETUP", "WATCH", "AVOID"})
GRADES_V1 = frozenset({"A+", "A", "B", "C", "D"})

_DEFINITION_KEYS = frozenset({"q", "paths", "grades", "min_composite", "tech", "fund"})

VOCABULARY_V2 = "stock_screen.v2"
VOCABULARIES = (VOCABULARY_V1, VOCABULARY_V2)

# Stock screen's stages (frontend pages/research/stocks/stockScreenStages.ts),
# the conditions each can evaluate today. `min` = the stage has an "at least N"
# whose ceiling is `max`; `kind` is how picked conditions combine.
STAGES_V2: dict[str, dict[str, Any]] = {
    "agree": {"kind": "agree", "max": 3, "conditions": ("m_sepa", "m_radar", "m_prem")},
    "trend": {"kind": "min", "max": 11, "conditions": tuple(sorted(TECH_CONDITIONS_V1))},
    "growth": {"kind": "min", "max": 8, "conditions": tuple(sorted(FUND_CONDITIONS_V1))},
    "momtier": {
        "kind": "min",
        "max": 10,
        "conditions": (
            "rsi_above_50",
            "rsi_healthy_range",
            "macd_bullish",
            "macd_strong",
            "roc_10_positive",
            "roc_21_positive",
            "rs_gt_spy",
            "volume_expanding",
            "volume_surge",
            "price_gt_sma10",
        ),
    },
    "radar": {"kind": "any", "conditions": ("grade_aplus", "grade_a", "grade_b", "grade_c", "grade_d")},
    "structure": {
        "kind": "any",
        "conditions": (
            "bb_squeeze",
            "bb_tight_squeeze",
            "adx_trending",
            "adx_strong_trend",
            "aroon_bullish",
            "aroon_up_strong",
            "vol_contracting",
            "vol_tight_contraction",
        ),
    },
    "sentiment": {
        "kind": "any",
        "conditions": (
            "si_declining",
            "low_short_float",
            "high_days_to_cover",
            "short_float_declining",
            "low_short_volume",
            "sv_ratio_declining",
        ),
    },
    # The three earnings windows read Research's estimated next print for every
    # name (/research/narrative/earnings/batch, 0.193.0, TD-158).
    "catalyst": {
        "kind": "any",
        "conditions": (
            "n8k_202_7d",
            "n8k_101_7d",
            "n8k_502_7d",
            "n8k_any_7d",
            "earn_lt_10d",
            "earn_10_30d",
            "earn_gt_10d",
        ),
    },
    "options": {"kind": "all", "conditions": ("ivr_ge_40", "ivr_ge_60", "vrp_pct_ge_70")},
}
PINE_WINDOWS_V2 = (1, 5, 10)
PINE_MATCH_V2 = ("any", "all")
UNIVERSES_V2 = ("all", "options", "watch", "book")
_PINE_PICK_RE = re.compile(r"^pine:([a-z][a-z0-9_]{1,47}):(buy|sell)$")
_DEFINITION_KEYS_V2 = frozenset({"stages", "pine", "universe"})

_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "definition",
    "vocabulary",
    "is_active",
    "origin_page",
    "created_at",
    "updated_at",
    "retired_at",
)

_SELECT_COLS = ", ".join(_COLUMNS)


class _Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def generate_screen_id(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")[:48] or "screen"
    ts = int(time.time() * 1000) & 0xFFFFFF
    return f"scr-{slug}-{ts:06x}{secrets.token_hex(2)}"


def _subset_of(value: Any, allowed: frozenset[str], what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"definition.{what} must be a list")
    items = [str(v) for v in value]
    unknown = [v for v in items if v not in allowed]
    if unknown:
        raise ValueError(f"unknown {what}: {', '.join(sorted(set(unknown)))}")
    # keep order, drop duplicates
    seen: set[str] = set()
    return [v for v in items if not (v in seen or seen.add(v))]


def _normalize_v1(raw: Any) -> dict[str, Any]:
    """Validate strictly against the v1 vocabulary; raise ValueError on drift."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("definition must be an object")
    unknown = set(raw.keys()) - _DEFINITION_KEYS
    if unknown:
        raise ValueError(f"unknown definition keys: {', '.join(sorted(unknown))}")
    min_composite = raw.get("min_composite", 0)
    if not isinstance(min_composite, (int, float)) or not 0 <= float(min_composite) <= 100:
        raise ValueError("definition.min_composite must be a number in 0–100")
    q = raw.get("q", "")
    if not isinstance(q, str):
        raise ValueError("definition.q must be a string")
    return {
        "q": q.strip(),
        "paths": _subset_of(raw.get("paths"), PATHS_V1, "paths"),
        "grades": _subset_of(raw.get("grades"), GRADES_V1, "grades"),
        "min_composite": float(min_composite),
        "tech": _subset_of(raw.get("tech"), TECH_CONDITIONS_V1, "tech"),
        "fund": _subset_of(raw.get("fund"), FUND_CONDITIONS_V1, "fund"),
    }


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != int(value):
        raise ValueError(f"{what} must be a whole number")
    n = int(value)
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be in {lo}–{hi}")
    return n


def _normalize_v2(raw: Any, known_pine: frozenset[str] | None) -> dict[str, Any]:
    """Validate strictly against the v2 vocabulary; raise ValueError on drift.

    ``known_pine`` is every script id in the Pine library (active or not): a
    pick naming a script the library has never held is drift. A script that
    is switched off still saves — the screen shows it as off, it does not
    drop it. ``None`` skips that one check (no database at hand).
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("definition must be an object")
    unknown = set(raw.keys()) - _DEFINITION_KEYS_V2
    if unknown:
        raise ValueError(f"unknown definition keys: {', '.join(sorted(unknown))}")

    stages_in = raw.get("stages") or {}
    if not isinstance(stages_in, Mapping):
        raise ValueError("definition.stages must be an object")
    bad_stages = set(stages_in.keys()) - set(STAGES_V2)
    if bad_stages:
        raise ValueError(f"unknown stages: {', '.join(sorted(bad_stages))}")
    stages: dict[str, dict[str, Any]] = {}
    for sid, cat in STAGES_V2.items():
        if sid not in stages_in:
            continue
        body = stages_in[sid]
        if not isinstance(body, Mapping):
            raise ValueError(f"definition.stages.{sid} must be an object")
        extra = set(body.keys()) - {"on", "min"}
        if extra:
            raise ValueError(f"unknown keys in stages.{sid}: {', '.join(sorted(extra))}")
        on = _subset_of(body.get("on"), frozenset(cat["conditions"]), f"stages.{sid}.on")
        out: dict[str, Any] = {"on": on}
        if "min" in body:
            if cat["kind"] not in ("min", "agree"):
                raise ValueError(f"stages.{sid} has no 'at least N'")
            ceiling = len(on) if cat["kind"] == "agree" and on else int(cat["max"])
            out["min"] = _int_in(body["min"], 0, ceiling, f"stages.{sid}.min")
        if out["on"] or out.get("min"):
            stages[sid] = out

    pine_in = raw.get("pine") or {}
    if not isinstance(pine_in, Mapping):
        raise ValueError("definition.pine must be an object")
    extra = set(pine_in.keys()) - {"on", "window", "match"}
    if extra:
        raise ValueError(f"unknown keys in pine: {', '.join(sorted(extra))}")
    picks_raw = pine_in.get("on")
    if picks_raw is None:
        picks_raw = []
    if not isinstance(picks_raw, Sequence) or isinstance(picks_raw, (str, bytes)):
        raise ValueError("definition.pine.on must be a list")
    picks: list[str] = []
    malformed: list[str] = []
    unknown_scripts: list[str] = []
    for p in (str(v) for v in picks_raw):
        m = _PINE_PICK_RE.match(p)
        if not m:
            malformed.append(p)
        elif known_pine is not None and m.group(1) not in known_pine:
            unknown_scripts.append(m.group(1))
        elif p not in picks:
            picks.append(p)
    if malformed:
        raise ValueError(f"malformed pine picks (want pine:<script>:buy|sell): {', '.join(malformed)}")
    if unknown_scripts:
        raise ValueError(f"unknown pine scripts: {', '.join(sorted(set(unknown_scripts)))}")
    window = _int_in(pine_in.get("window", 5), 1, 10, "pine.window")
    if window not in PINE_WINDOWS_V2:
        raise ValueError(f"pine.window must be one of {', '.join(map(str, PINE_WINDOWS_V2))}")
    match = pine_in.get("match", "any")
    if match not in PINE_MATCH_V2:
        raise ValueError("pine.match must be any or all")

    universe = raw.get("universe")
    if universe is not None and universe not in UNIVERSES_V2:
        raise ValueError(f"unknown universe: {universe} (one of {', '.join(UNIVERSES_V2)})")

    return {"stages": stages, "pine": {"on": picks, "window": window, "match": match}, "universe": universe}


def normalize_definition(
    raw: Any, vocabulary: str = VOCABULARY_V1, *, known_pine: frozenset[str] | None = None
) -> dict[str, Any]:
    """Validate ``raw`` strictly against ``vocabulary``; raise ValueError on drift."""
    if vocabulary == VOCABULARY_V1:
        return _normalize_v1(raw)
    if vocabulary == VOCABULARY_V2:
        return _normalize_v2(raw, known_pine)
    raise ValueError(f"unknown vocabulary: {vocabulary} (one of {', '.join(VOCABULARIES)})")


def known_pine_scripts(conn: _Connection) -> frozenset[str]:
    """Every script id the Pine library holds, active or not."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {TABLE_RESEARCH_PINE_SCRIPT}")
        rows = cur.fetchall()
    return frozenset(str(r[0] if not isinstance(r, Mapping) else r["id"]) for r in rows)


def vocabulary_catalog(known_pine: Sequence[str] = ()) -> dict[str, Any]:
    """What each stamp accepts — read by the frontend so its conditions cannot drift from these."""
    return {
        "versions": list(VOCABULARIES),
        VOCABULARY_V1: {
            "keys": sorted(_DEFINITION_KEYS),
            "paths": sorted(PATHS_V1),
            "grades": sorted(GRADES_V1),
            "tech": sorted(TECH_CONDITIONS_V1),
            "fund": sorted(FUND_CONDITIONS_V1),
            "min_composite": [0, 100],
        },
        VOCABULARY_V2: {
            "keys": sorted(_DEFINITION_KEYS_V2),
            "stages": {
                sid: {"kind": cat["kind"], "max": cat.get("max"), "conditions": list(cat["conditions"])}
                for sid, cat in STAGES_V2.items()
            },
            "pine": {"windows": list(PINE_WINDOWS_V2), "match": list(PINE_MATCH_V2), "scripts": sorted(known_pine)},
            "universes": list(UNIVERSES_V2),
        },
    }


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        out = {col: row[col] for col in _COLUMNS if col in row}
    else:
        out = {_COLUMNS[i]: row[i] for i in range(min(len(_COLUMNS), len(row)))}
    val = out.get("definition")
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        try:
            out["definition"] = json.loads(val)
        except Exception:
            pass
    for col in ("created_at", "updated_at", "retired_at"):
        if col in out:
            out[col] = _iso(out[col])
    return out


def create_screen(
    conn: _Connection,
    *,
    name: str,
    definition: Any,
    description: str | None = None,
    origin_page: str | None = None,
    vocabulary: str = VOCABULARY_V1,
) -> dict[str, Any]:
    known = known_pine_scripts(conn) if vocabulary == VOCABULARY_V2 else None
    clean = normalize_definition(definition, vocabulary, known_pine=known)
    screen_id = generate_screen_id(name)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_SAVED_SCREEN}
                (id, name, description, definition, vocabulary, origin_page)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_SELECT_COLS}
            """,
            (screen_id, name, description, json.dumps(clean), vocabulary, origin_page),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row)


def list_screens(conn: _Connection, *, include_retired: bool = False) -> list[dict[str, Any]]:
    where = "" if include_retired else "WHERE retired_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_SELECT_COLS} FROM {TABLE_RESEARCH_SAVED_SCREEN}
            {where}
            ORDER BY updated_at DESC
            """
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def get_screen(conn: _Connection, screen_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM {TABLE_RESEARCH_SAVED_SCREEN} WHERE id = %s",
            (screen_id,),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def patch_screen(conn: _Connection, screen_id: str, updates: Mapping[str, Any]) -> dict[str, Any] | None:
    """Update name / description / definition / is_active; bumps updated_at.

    A definition is validated against the stamp it is sent with
    (``updates["vocabulary"]``) or, without one, the row's own stamp; a new
    stamp without a definition is refused, since the old definition does not
    speak it.
    """
    sets: list[str] = []
    params: list[Any] = []
    if "vocabulary" in updates and "definition" not in updates:
        raise ValueError("a new vocabulary needs its definition in the same request")
    for key in ("name", "description", "origin_page"):
        if key in updates:
            sets.append(f"{key} = %s")
            params.append(updates[key])
    if "definition" in updates:
        vocabulary = updates.get("vocabulary")
        if vocabulary is None:
            current = get_screen(conn, screen_id)
            if current is None:
                return None
            vocabulary = current.get("vocabulary") or VOCABULARY_V1
        known = known_pine_scripts(conn) if vocabulary == VOCABULARY_V2 else None
        sets.append("definition = %s")
        params.append(json.dumps(normalize_definition(updates["definition"], vocabulary, known_pine=known)))
        if "vocabulary" in updates:
            sets.append("vocabulary = %s")
            params.append(vocabulary)
    if "is_active" in updates:
        sets.append("is_active = %s")
        params.append(bool(updates["is_active"]))
    if not sets:
        return get_screen(conn, screen_id)
    sets.append("updated_at = now()")
    params.append(screen_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_SAVED_SCREEN} SET {", ".join(sets)}
            WHERE id = %s
            RETURNING {_SELECT_COLS}
            """,
            tuple(params),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row) if row else None


def retire_screen(conn: _Connection, screen_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_RESEARCH_SAVED_SCREEN}
            SET retired_at = now(), is_active = false, updated_at = now()
            WHERE id = %s AND retired_at IS NULL
            RETURNING {_SELECT_COLS}
            """,
            (screen_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_dict(row) if row else None
