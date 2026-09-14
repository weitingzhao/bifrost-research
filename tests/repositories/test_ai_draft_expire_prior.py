"""D7 — expire prior pending digests in the same insert_draft transaction."""

from __future__ import annotations

from typing import Any

from bifrost_research.repositories import ai_draft as draft_repo


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._last: Any = None

    def execute(self, query: str, params: Any = None) -> None:
        self._conn.statements.append(" ".join(query.split()))
        self._conn.params.append(params)
        q = " ".join(query.split()).lower()
        if q.startswith("update") and "status = 'expired'" in q:
            kind, exclude = params
            for did, row in list(self._conn.drafts.items()):
                if row["kind"] == kind and row["status"] == "pending" and did != exclude:
                    row["status"] = "expired"
            self._last = None
            return
        if "insert into" in q and "ai_draft" in q:
            row = {
                "id": params[0],
                "kind": params[1],
                "payload": params[2],
                "scope": params[3],
                "status": params[4],
                "generated_by": params[5],
                "linked_action_id": params[6],
                "created_at": "2026-09-14T00:00:00+00:00",
                "expires_at": params[7],
            }
            self._conn.drafts[params[0]] = row
            self._last = tuple(row[c] for c in draft_repo._COLUMNS)
            return
        self._last = None

    def fetchone(self) -> Any:
        return self._last

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[Any] = []
        self.drafts: dict[str, dict[str, Any]] = {
            "old": {
                "id": "old",
                "kind": "daily_digest",
                "payload": "{}",
                "scope": "digest:2026-09-13",
                "status": "pending",
                "generated_by": "digest_agent",
                "linked_action_id": None,
                "created_at": "2026-09-13T11:30:00+00:00",
                "expires_at": None,
            },
            "other_kind": {
                "id": "other_kind",
                "kind": "draft_verdict",
                "payload": "{}",
                "scope": "digest:2026-09-14",
                "status": "pending",
                "generated_by": "eod",
                "linked_action_id": None,
                "created_at": "2026-09-13T00:00:00+00:00",
                "expires_at": None,
            },
        }

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_insert_draft_expire_prior_pending_across_days() -> None:
    # A same-scope pair cannot catch the production bug: digest_scope is
    # digest:YYYY-MM-DD, so yesterday never matches today's INSERT.
    conn = _Conn()
    new = draft_repo.insert_draft(
        conn,
        kind="daily_digest",
        payload={"markdown": "# today"},
        scope="digest:2026-09-14",
        generated_by="digest_agent",
        expire_prior_pending=True,
    )
    assert conn.drafts["old"]["status"] == "expired"
    assert conn.drafts["other_kind"]["status"] == "pending"
    assert new["status"] == "pending"
    assert conn.drafts["old"]["scope"] != new["scope"]
    expire_params = next(
        p for p, s in zip(conn.params, conn.statements) if "status = 'expired'" in s.lower()
    )
    assert expire_params == ("daily_digest", new["id"])
    expire_i = next(i for i, s in enumerate(conn.statements) if "status = 'expired'" in s.lower())
    insert_i = next(i for i, s in enumerate(conn.statements) if "insert into" in s.lower())
    assert expire_i < insert_i


def test_insert_draft_without_flag_leaves_prior() -> None:
    conn = _Conn()
    draft_repo.insert_draft(
        conn,
        kind="daily_digest",
        payload={"markdown": "# today"},
        scope="digest:2026-09-14",
        generated_by="digest_agent",
    )
    assert conn.drafts["old"]["status"] == "pending"
    assert not any("status = 'expired'" in s.lower() for s in conn.statements)
