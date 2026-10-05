"""The Pine script library: built-in scripts shipped with Research, and the table that holds all of them.

Built-ins (``library/*.pine``) are Bifrost's own Pine v5 implementations of
public, well-known rules, so they can live in this public repository. A
community or user script is pasted into ``research.pine_script`` through the
API and never enters the repository.

Contract every script keeps: a plot or plotshape titled ``buy`` and/or
``sell`` that is true on the session the signal fires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bifrost_research.schema.schemas import TABLE_RESEARCH_PINE_SCRIPT

LIBRARY_DIR = Path(__file__).with_name("library")
BUILTIN_LICENSE = "Bifrost original implementation"
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_TITLE_RE = re.compile(r"""(?:indicator|strategy)\(\s*["']([^"']+)["']""")
_SIGNAL_RE = re.compile(r"""(?:plot|plotshape|plotchar)\([^\n]*["'](buy|sell)["']""")
MAX_SOURCE = 200_000

_COLS = (
    "id, name, source, version, origin, license, source_url, notes, is_active, created_at, updated_at"
)


@dataclass(frozen=True)
class PineScript:
    id: str
    name: str
    source: str
    version: int = 1
    origin: str = "user"
    license: str | None = None
    source_url: str | None = None
    notes: str | None = None
    is_active: bool = True

    def to_dict(self, *, with_source: bool = True) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "origin": self.origin,
            "license": self.license,
            "source_url": self.source_url,
            "notes": self.notes,
            "is_active": self.is_active,
            "signals": signal_sides(self.source),
        }
        if with_source:
            out["source"] = self.source
        return out


def signal_sides(source: str) -> list[str]:
    """Which of ``buy`` / ``sell`` the script plots, by title."""
    return sorted(set(_SIGNAL_RE.findall(source or "")))


def validate(script_id: str, name: str, source: str) -> None:
    if not _ID_RE.match(script_id or ""):
        raise ValueError("id: 2–48 characters, lower-case letters, digits and _ , starting with a letter")
    if not (name or "").strip():
        raise ValueError("name is required")
    if not (source or "").strip():
        raise ValueError("source is required")
    if len(source) > MAX_SOURCE:
        raise ValueError(f"source longer than {MAX_SOURCE} characters")
    if not signal_sides(source):
        raise ValueError('the script must plot a series titled "buy" and/or "sell"')


def builtin_scripts() -> list[PineScript]:
    out: list[PineScript] = []
    for path in sorted(LIBRARY_DIR.glob("*.pine")):
        src = path.read_text(encoding="utf-8")
        m = _TITLE_RE.search(src)
        name = m.group(1).replace("Bifrost · ", "") if m else path.stem
        out.append(PineScript(id=path.stem, name=name, source=src, origin="bifrost", license=BUILTIN_LICENSE))
    return out


def _row(r: Any) -> PineScript:
    if not isinstance(r, Mapping):
        keys = [c.strip() for c in _COLS.split(",")]
        r = dict(zip(keys, r))
    return PineScript(
        id=r["id"],
        name=r["name"],
        source=r["source"],
        version=int(r["version"]),
        origin=r["origin"],
        license=r.get("license"),
        source_url=r.get("source_url"),
        notes=r.get("notes"),
        is_active=bool(r["is_active"]),
    )


def list_scripts(conn: Any, *, active_only: bool = False) -> list[PineScript]:
    where = "WHERE is_active" if active_only else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM {TABLE_RESEARCH_PINE_SCRIPT} {where} ORDER BY origin, id")
        return [_row(r) for r in cur.fetchall() or []]


def get_script(conn: Any, script_id: str) -> PineScript | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM {TABLE_RESEARCH_PINE_SCRIPT} WHERE id = %s", (script_id,))
        r = cur.fetchone()
    return _row(r) if r else None


def upsert_script(conn: Any, s: PineScript) -> PineScript:
    """Insert, or update in place; the version moves only when the source changes."""
    validate(s.id, s.name, s.source)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_RESEARCH_PINE_SCRIPT}
                (id, name, source, version, origin, license, source_url, notes, is_active)
            VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                version = {TABLE_RESEARCH_PINE_SCRIPT}.version
                    + CASE WHEN {TABLE_RESEARCH_PINE_SCRIPT}.source IS DISTINCT FROM EXCLUDED.source
                           THEN 1 ELSE 0 END,
                source = EXCLUDED.source,
                origin = EXCLUDED.origin,
                license = EXCLUDED.license,
                source_url = EXCLUDED.source_url,
                notes = EXCLUDED.notes,
                is_active = EXCLUDED.is_active,
                updated_at = now()
            RETURNING {_COLS}
            """,
            (s.id, s.name, s.source, s.origin, s.license, s.source_url, s.notes, s.is_active),
        )
        row = cur.fetchone()
    conn.commit()
    return _row(row)


def ensure_builtins(conn: Any, scripts: Sequence[PineScript] | None = None) -> int:
    """Seed or refresh the built-ins. A built-in the Owner switched off stays off."""
    n = 0
    for s in scripts if scripts is not None else builtin_scripts():
        current = get_script(conn, s.id)
        if current is not None and current.origin != "bifrost":
            continue  # the id was taken by a user script; leave it alone
        keep_active = current.is_active if current is not None else True
        if current is None or current.source != s.source or current.name != s.name:
            upsert_script(conn, PineScript(**{**s.__dict__, "is_active": keep_active}))
            n += 1
    return n
