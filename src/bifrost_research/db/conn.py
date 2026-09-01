"""PostgreSQL connection to bifrost_golden_source."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Generator


def connect_kwargs() -> dict[str, Any]:
    """Build psycopg2/psycopg connect kwargs from ANALYTICS_PG_* / POSTGRES_* env."""
    host = os.environ.get("ANALYTICS_PG_HOST") or os.environ.get("POSTGRES_HOST") or "192.168.10.73"
    port = int(os.environ.get("ANALYTICS_PG_PORT") or os.environ.get("POSTGRES_PORT") or "30432")
    dbname = (
        os.environ.get("ANALYTICS_PG_DATABASE")
        or os.environ.get("POSTGRES_DB")
        or "bifrost_golden_source"
    )
    user = (
        os.environ.get("ANALYTICS_PG_USER")
        or os.environ.get("POSTGRES_USER")
        or "analytics_writer"
    )
    password = (
        os.environ.get("ANALYTICS_PG_PASSWORD")
        or os.environ.get("POSTGRES_PASSWORD")
        or ""
    )
    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "connect_timeout": 15,
    }


def connect() -> Any:
    """Open a new connection (caller must close). Prefers psycopg2, falls back to psycopg."""
    kwargs = connect_kwargs()
    try:
        import psycopg2

        return psycopg2.connect(**kwargs)
    except ImportError:
        import psycopg

        # psycopg3 uses dbname → not always; map keys
        return psycopg.connect(
            host=kwargs["host"],
            port=kwargs["port"],
            dbname=kwargs["dbname"],
            user=kwargs["user"],
            password=kwargs["password"],
            connect_timeout=kwargs["connect_timeout"],
        )


@contextmanager
def get_conn() -> Generator[Any, None, None]:
    """Yield a connection and close on exit."""
    conn = connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def rollback_quietly(conn: Any) -> None:
    """Reset a connection after a caught query error.

    Postgres aborts the whole transaction on the first failed statement: every
    later query returns InFailedSqlTransaction until someone rolls back. A
    fail-soft handler that returns an empty list without this does not degrade
    gracefully — it takes down every query that follows while logging as though
    the error were contained.
    """
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001, S110
        pass
