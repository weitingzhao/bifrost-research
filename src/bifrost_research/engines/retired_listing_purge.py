"""Delete what the derived layer computed for a listing after it stopped trading.

Two names in this universe stopped trading in August 2026 without leaving a
successor that is the same security:

- AVB merged into Equity Residential, which became Vivmark Residential (VMRK),
  at 2.793 VMRK per AVB share. Its last close is 2026-08-14.
- WBS was acquired by Banco Santander. Its last close is 2026-08-19.

Neither can be relabelled onto another symbol the way ``rename_history_move``
does for SATS -> ECHO: CIK and composite FIGI differ, and a 2.793:1 exchange
cannot be carried onto an option chain. OCC's adjusted AVB contracts do exist,
as the ``VMRK1`` root at 279 shares per contract. They are a different
deliverable, so they cannot join VMRK's standard chain either.

What kept arriving under the dead labels was a frozen residue. Measured
2026-09-27 for 2026-09-08..09-25, every one of the 14 sessions of ``O:AVB…``
snapshots had its last trade on or before 2026-08-14, and closes, open interest
and volume were identical from session to session. Only IV and delta moved: the
vendor re-solves IV from a fixed premium against a shrinking time to expiry.
The 2026-10-16 fit's ATM vol climbed from 0.2415 at 37 days to 0.3182 at 21, a
ratio of about sqrt(37/21). Its slope stayed at 0.048, the August shape, and
ranked 18th among steepest skews on 2026-09-25. With no close, the surface fit
also invented a spot, 185, from the frozen deltas. WBS is the same case.

The measurement counted 2,027 derived rows dated after each name's last bar:
AVB 1,033 across 16 tables and WBS 994 across 17. They cover the option side
(surface fit / iv / residual, max pain, PCR, vanna/charm, flow) and the stock
side (forecast, terrain, momentum, playbook, scan, SEPA). The stock side exists
because engines iterate ``research.option_universe`` whether or not the name
traded that session.

This only purges. Nothing is relabelled, because no successor is the same
security, and nothing is recomputed, because there is no market left to
compute from. The last bar's own session stays; it was a real trading day.
``raw_market`` is left alone. Those rows are exactly what the vendor answered,
and they are the evidence for everything above.

Two gates keep it from recurring. Plugin 0.48.0 stops snapshotting a retired
listing. Research 0.134.0 (live in Dagster from 0.139.0-dagster) drops a core or
edge name with no close in ``LIVENESS_DAYS`` from the universe.

Tables are discovered from the catalog, not listed by hand. Every ``features``
table with both ``symbol`` and ``trade_date`` is examined, so a table added
later cannot be missed. ``stock_signal_canonical_pnl_daily`` has no
``trade_date`` column and keeps its own repair path.

Usage::

    python -m bifrost_research.engines.retired_listing_purge
    python -m bifrost_research.engines.retired_listing_purge --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from bifrost_research.db.conn import connect
from bifrost_research.engines.option_universe.entry import LIVENESS_DAYS

logger = logging.getLogger(__name__)

#: The listings to purge. A curated list, because this is a one-off over a
#: measured set that the Owner approved on 2026-09-27. The gates in
#: :func:`retirement` still have to agree before anything is touched.
RETIRED: tuple[str, ...] = ("AVB", "WBS")

_TABLES_SQL = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'features'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND EXISTS (SELECT 1 FROM pg_attribute a
              WHERE a.attrelid = c.oid AND a.attname = 'symbol' AND NOT a.attisdropped)
  AND EXISTS (SELECT 1 FROM pg_attribute a
              WHERE a.attrelid = c.oid AND a.attname = 'trade_date' AND NOT a.attisdropped)
ORDER BY c.relname
"""

_EVIDENCE_SQL = """
SELECT (SELECT max(bar_date) FROM raw_market.stock_daily WHERE symbol = %s) AS last_bar,
       (SELECT active FROM raw_market.ticker WHERE symbol = %s) AS active,
       (SELECT delisted_utc FROM raw_market.ticker WHERE symbol = %s) AS delisted_utc
"""

#: A table name is interpolated into SQL, so only plain lower-case identifiers pass.
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _first_row(cur: Any) -> tuple[Any, ...] | None:
    rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    for row in rows or []:
        return tuple(row.values()) if hasattr(row, "values") else tuple(row)
    return None


def feature_tables(conn: Any) -> list[str]:
    """Every ``features`` table keyed by ``symbol`` and ``trade_date`` (partition parents only)."""
    with conn.cursor() as cur:
        cur.execute(_TABLES_SQL)
        rows = cur.fetchall() if hasattr(cur, "fetchall") else []
    out: list[str] = []
    for row in rows or []:
        name = str(next(iter(row.values())) if hasattr(row, "values") else row[0])
        if not _IDENT.match(name):
            raise ValueError(f"refusing to interpolate table name {name!r}")
        out.append(name)
    return out


def retirement(conn: Any, symbol: str, *, today: date) -> dict[str, Any]:
    """The last bar, and whether the listing is retired.

    Two signals have to agree, as they must in the Plugin's skip: the reference
    row says inactive, and there has been no close for ``LIVENESS_DAYS``. A
    name that trades again, or was never marked inactive, is refused.
    """
    with conn.cursor() as cur:
        cur.execute(_EVIDENCE_SQL, (symbol, symbol, symbol))
        row = _first_row(cur) or (None, None, None)
    last_bar, active, delisted = row[0], row[1], row[2]
    out: dict[str, Any] = {"last_bar": last_bar, "active": active, "delisted_utc": delisted}
    if last_bar is None:
        out["refused"] = "no_bars"
    elif active is None:
        out["refused"] = "no_ticker_row"
    elif active:
        out["refused"] = "still_active"
    elif last_bar > today - timedelta(days=LIVENESS_DAYS):
        out["refused"] = "traded_recently"
    return out


def purge(
    conn: Any,
    symbols: Sequence[str],
    *,
    apply: bool,
    today: date | None = None,
) -> dict[str, Any]:
    """Rows dated strictly after each retired listing's last bar, across every table.

    Counting only unless ``apply``. Applied, it is one transaction: every delete
    commits together or none does.
    """
    today = today or datetime.now(timezone.utc).date()
    tables = feature_tables(conn)
    per_symbol: dict[str, Any] = {}
    try:
        for symbol in symbols:
            ev = retirement(conn, symbol, today=today)
            entry: dict[str, Any] = dict(ev)
            if "refused" in ev:
                logger.warning("%s refused: %s", symbol, ev["refused"])
                per_symbol[symbol] = entry
                continue
            cut = ev["last_bar"]
            rows: dict[str, int] = {}
            for table in tables:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT count(*) FROM features.{table} WHERE symbol = %s AND trade_date > %s",
                        (symbol, cut),
                    )
                    n = int((_first_row(cur) or (0,))[0] or 0)
                    if n == 0:
                        continue
                    if apply:
                        cur.execute(
                            f"DELETE FROM features.{table} WHERE symbol = %s AND trade_date > %s",
                            (symbol, cut),
                        )
                        n = int(getattr(cur, "rowcount", 0) or 0)
                rows[table] = n
            entry["rows"] = rows
            entry["tables"] = len(rows)
            entry["total"] = sum(rows.values())
            per_symbol[symbol] = entry
        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
    return {
        "applied": apply,
        "tables_examined": len(tables),
        "symbols": per_symbol,
        "total": sum(int(v.get("total", 0)) for v in per_symbol.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="delete; without it the run only counts"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect()
    try:
        out = {"engine": "retired_listing_purge", **purge(conn, RETIRED, apply=args.apply)}
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    # A refused symbol is a reported outcome: the evidence did not agree, so the
    # run left it alone rather than guessing.
    return 0


if __name__ == "__main__":
    sys.exit(main())
