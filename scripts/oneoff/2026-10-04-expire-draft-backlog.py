#!/usr/bin/env python3
"""One-off: expire the pending-draft backlog in ``research.ai_draft`` (0.166.0, D7).

REQUEST-research-draft-expiry-2026-10-04 §6. The rules are the ones the
release runs every EOD (``repositories/draft_expiry.py``) — this script only
applies them once to the rows written before 0.166.0, plus the D4 backfill:
action rows of drafts that are already ``expired`` but still ``proposed``
(the 19 daily_digest rows expired by the old ``expire_prior_pending``).

Writes ``research.ai_draft`` (status + ``payload.expired``) and
``research.ai_action_log`` (status) only. Never touches
``research.candidate_pool`` (D8) — it is read to decide, never written.

Run by the Owner, in this order (see the REQUEST §6 for the full procedure):

    python scripts/oneoff/2026-10-04-expire-draft-backlog.py                 # --dry-run (default, read-only)
    python scripts/oneoff/2026-10-04-expire-draft-backlog.py --backup        # read-only export + SHA256SUMS + README
    python scripts/oneoff/2026-10-04-expire-draft-backlog.py --commit --expect N

Connection: ``ANALYTICS_PG_*`` (or ``POSTGRES_*``) as every Research job. The
dry run and the backup open the session read-only
(``default_transaction_read_only = on``), so they may point at a replica. The
password is never printed. D10 BLOCKED — nothing here is a trading path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bifrost_research.db.conn import connect, connect_kwargs  # noqa: E402
from bifrost_research.repositories import draft_expiry as ex  # noqa: E402

BY = "oneoff:2026-10-04-draft-backlog"
BACKUP_ROOT = Path.home() / "bifrost-backups" / "golden-source"

#: REQUEST §6, measured 2026-10-05 00:10 UTC on the replica.
EXPECTED_6 = {
    "eod_verdict": 660,
    "candidate_batch": 38,
    "policy_suggestion": 31,
    "decision_draft": 26,
    "order_intent": 10,
    "playbook_note": 0,
    "daily_digest": 0,
}

_STALE_ACTIONS_SQL = """
    SELECT d.kind, COUNT(*)
    FROM research.ai_draft d
    JOIN research.ai_action_log a ON a.id = d.linked_action_id
    WHERE d.status = 'expired' AND a.status = 'proposed'
    GROUP BY d.kind
"""

_STALE_ACTIONS_UPDATE = """
    UPDATE research.ai_action_log a
    SET status = 'expired'
    FROM research.ai_draft d
    WHERE d.linked_action_id = a.id
      AND d.status = 'expired'
      AND a.status = 'proposed'
"""


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _target() -> str:
    k = connect_kwargs()
    return f"{k['user']}@{k['host']}:{k['port']}/{k['dbname']}"


def _read_only(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        row = cur.fetchone()
    if str(row[0]).lower() != "on":
        raise SystemExit("could not open a read-only session; refusing to continue")


def _pool_counts(conn: Any) -> dict[str, int]:
    """research.candidate_pool by status — printed before and after so D8 can be checked."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM research.candidate_pool GROUP BY status ORDER BY status")
        return {str(k): int(n) for k, n in cur.fetchall() or []}


def _stale_actions(conn: Any) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(_STALE_ACTIONS_SQL)
        return {str(k): int(n) for k, n in cur.fetchall() or []}


def _report(plan: list[ex.Expiry], pending_by_kind: dict[str, int], stale: dict[str, int], *, now: datetime, verbose: bool) -> int:
    summary = ex.summarize(plan)
    expiring = Counter(e.kind for e in plan)
    kinds = sorted(set(pending_by_kind) | set(EXPECTED_6))
    _say(f"target      {_target()}")
    _say(f"as of       {now.isoformat()}")
    _say()
    _say(f"{'kind':<20}{'pending':>9}{'expire':>9}{'§6':>7}{'diff':>7}{'remain':>9}   reasons")
    for kind in kinds:
        n_pending = pending_by_kind.get(kind, 0)
        n_exp = expiring.get(kind, 0)
        want = EXPECTED_6.get(kind)
        diff = "" if want is None else f"{n_exp - want:+d}"
        reasons = ", ".join(f"{r} {n}" for r, n in summary["by_kind"].get(kind, {}).items())
        _say(f"{kind:<20}{n_pending:>9}{n_exp:>9}{'' if want is None else want:>7}{diff:>7}{n_pending - n_exp:>9}   {reasons}")
    total_pending = sum(pending_by_kind.values())
    _say(f"{'total':<20}{total_pending:>9}{len(plan):>9}{sum(EXPECTED_6.values()):>7}{len(plan) - sum(EXPECTED_6.values()):>+7d}{total_pending - len(plan):>9}")
    _say()
    _say(f"by reason   {json.dumps(summary['by_reason'])}")
    _say(f"D4 backfill already-expired drafts whose action row is still 'proposed': {json.dumps(stale)} (total {sum(stale.values())})")
    _say("candidate_pool: read only — no write is issued (D8)")
    if verbose:
        _say()
        for e in plan:
            if e.kind == "eod_verdict" and e.reason == ex.REASON_SUPERSEDED:
                continue
            _say(f"  {e.id}  {e.kind:<18} {e.reason:<20} {json.dumps({'superseded_by': e.superseded_by, **e.detail}, default=str)}")
        _say("  (eod_verdict superseded rows omitted)")
    return len(plan)


def dry_run(*, verbose: bool) -> int:
    conn = connect()
    try:
        _read_only(conn)
        now = datetime.now(timezone.utc)
        plan, pending = ex.collect_plan(conn, now=now, strict=True)
        stale = _stale_actions(conn)
        pool = _pool_counts(conn)
        conn.rollback()
        n = _report(plan, pending, stale, now=now, verbose=verbose)
        _say(f"candidate_pool by status (read): {json.dumps(pool)}")
        _say()
        _say(f"dry run: {n} drafts would expire. Nothing was written.")
        return 0
    finally:
        conn.close()


def _copy_csv(conn: Any, sql: str, path: Path) -> int:
    buf = io.StringIO()
    with conn.cursor() as cur:
        cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)", buf)
    data = buf.getvalue()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    return max(0, sum(1 for _ in csv.reader(io.StringIO(data))) - 1)


def backup(dest: Path) -> int:
    if dest.exists():
        raise SystemExit(f"{dest} already exists; refusing to overwrite a backup")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.mkdir(mode=0o700)
    conn = connect()
    try:
        _read_only(conn)
        now = datetime.now(timezone.utc)
        files: dict[str, int] = {}
        files["ai_draft.csv"] = _copy_csv(conn, "SELECT * FROM research.ai_draft ORDER BY created_at, id", dest / "ai_draft.csv")
        files["ai_action_log_linked.csv"] = _copy_csv(
            conn,
            "SELECT a.* FROM research.ai_action_log a "
            "WHERE a.id IN (SELECT linked_action_id FROM research.ai_draft WHERE linked_action_id IS NOT NULL) "
            "ORDER BY a.created_at, a.id",
            dest / "ai_action_log_linked.csv",
        )
        plan, pending = ex.collect_plan(conn, now=now, strict=True)
        stale = _stale_actions(conn)
        conn.rollback()
    finally:
        conn.close()
    plan_doc = {
        "as_of": now.isoformat(),
        "by": BY,
        "pending_by_kind": pending,
        "summary": ex.summarize(plan),
        "stale_actions_by_kind": stale,
        "rows": [
            {"id": e.id, "kind": e.kind, "reason": e.reason, "superseded_by": e.superseded_by, "linked_action_id": e.linked_action_id, **e.detail}
            for e in plan
        ],
    }
    fd = os.open(dest / "plan.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(plan_doc, fh, indent=1, default=str)
    files["plan.json"] = len(plan)

    sums = []
    for name in sorted(files):
        digest = hashlib.sha256((dest / name).read_bytes()).hexdigest()
        sums.append(f"{digest}  {name}")
    fd = os.open(dest / "SHA256SUMS", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sums) + "\n")

    readme = f"""# {dest.name}

Before the one-off expiry of the pending-draft backlog (bifrost-research 0.166.0,
REQUEST-research-draft-expiry-2026-10-04 §6, Owner D7). Taken {now.isoformat()}
from {_target()} in a read-only session.

| file | rows | what |
|---|---:|---|
| ai_draft.csv | {files['ai_draft.csv']} | `research.ai_draft`, whole table |
| ai_action_log_linked.csv | {files['ai_action_log_linked.csv']} | `research.ai_action_log` rows any draft links to |
| plan.json | {files['plan.json']} | the drafts the script planned to expire at this moment, with reasons |

Verify: `cd {dest} && shasum -a 256 -c SHA256SUMS`

Restore (a PROD write — Owner approval first): for every id in plan.json that
the commit expired, set `status = 'pending'` and `payload = payload - 'expired'`
in `research.ai_draft`, and set the linked `research.ai_action_log` row back to the
status in ai_action_log_linked.csv. Nothing else was touched (no candidate_pool,
hypothesis or objective rows).
"""
    fd = os.open(dest / "README.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(readme)

    root_readme = dest.parent / "README.md"
    with open(root_readme, "a", encoding="utf-8") as fh:
        fh.write(
            f"\n## {dest.name}\n\n`research.ai_draft` ({files['ai_draft.csv']} rows) and linked "
            f"`research.ai_action_log` ({files['ai_action_log_linked.csv']} rows) as CSV, plus the expiry plan "
            f"({files['plan.json']} drafts), before the 0.166.0 backlog expiry. Taken {now.isoformat()}. "
            "See the folder's README.md for restore.\n"
        )
    _say(f"backup written to {dest}")
    for line in sums:
        _say(f"  {line}")
    _say(f"plan at backup time: {len(plan)} drafts would expire")
    return 0


def commit(expect: int) -> int:
    conn = connect()
    try:
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            if str(cur.fetchone()[0]).lower() == "on":
                raise SystemExit("this session is read-only (a replica?); --commit needs the primary")
        plan, pending = ex.collect_plan(conn, now=now, strict=True)
        stale = _stale_actions(conn)
        pool_before = _pool_counts(conn)
        n = _report(plan, pending, stale, now=now, verbose=False)
        if n != expect:
            conn.rollback()
            _say()
            _say(f"ABORT: the plan has {n} drafts, --expect said {expect}. Nothing was written.")
            return 2
        with conn.cursor() as cur:
            applied = ex.apply_with_cursor(cur, plan, by=BY, at=now)
            cur.execute(_STALE_ACTIONS_UPDATE)
            backfilled = int(cur.rowcount or 0)
        if applied["expired"] != expect:
            conn.rollback()
            _say(f"ABORT: {applied['expired']} rows changed, expected {expect} (a row moved underneath). Rolled back.")
            return 3
        pool_after = _pool_counts(conn)
        conn.commit()
        # This script issues no candidate_pool statement (D8). A difference here
        # can only come from another writer (the pool's own TTL on a page load).
        _say(f"candidate_pool by status before {json.dumps(pool_before)} / after {json.dumps(pool_after)}")
        _say()
        _say(
            f"committed: {applied['expired']} drafts expired, {applied['actions_expired']} linked action rows "
            f"expired with them, {backfilled} action rows of already-expired drafts backfilled (D4)."
        )
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only: plan, count per kind, reconcile with §6 (default)")
    mode.add_argument(
        "--backup",
        nargs="?",
        const="",
        metavar="DIR",
        help=f"read-only export to DIR (default {BACKUP_ROOT}/<today>_draft-expiry)",
    )
    mode.add_argument("--commit", action="store_true", help="expire the planned drafts in one transaction")
    ap.add_argument("--expect", type=int, help="with --commit: the dry run's count; any other count aborts")
    ap.add_argument("--verbose", action="store_true", help="list every planned row except superseded eod_verdicts")
    args = ap.parse_args()

    if args.commit:
        if args.expect is None:
            ap.error("--commit needs --expect N (the count from the dry run)")
        return commit(args.expect)
    if args.backup is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        dest = Path(args.backup).expanduser() if args.backup else BACKUP_ROOT / f"{today}_draft-expiry"
        return backup(dest)
    return dry_run(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
