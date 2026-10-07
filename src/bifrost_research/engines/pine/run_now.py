"""Run one Pine script's build now, from the API (ledger S14, Owner 2026-10-06, option A).

A saved script used to wait for the 22:30 ET batch before the chart, the
screener or Signal Decay could show it. ``start`` runs ``build.run`` for that
one script on a background thread of the research-api process — the same
pattern as the harness Run dialog — and returns at once; ``get`` and
``latest`` read its progress. A new or edited script is rebuilt over the whole
history (about a minute for the universe), an unchanged one rewrites its
recent sessions.

One run at a time in this process. The build's own lock on ``pine-build:<id>``
keeps a run and the nightly batch off the same script: the run steps aside
(``skipped``) instead of waiting. Jobs live in memory only — an API restart
forgets them, and the script is simply run again. research-api runs one
replica (``k8s/api/deployment.yaml``); with more, a poll could land on a pod
that never saw the job.

Measured 2026-10-06 on the cluster (dry run, image 0.200.0, 713 names): a
whole-history build of supertrend took 41 s and raised the process peak by
about 165 MB; a script reading two context series took 67 s within the same
peak. The API container's limit is 512 Mi and it idles near 112 MB.

D10 BLOCKED — signals only.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from bifrost_research.engines.pine import build

logger = logging.getLogger(__name__)

#: Jobs remembered for ``get`` / ``latest``.
KEEP = 50


@dataclass
class RunJob:
    id: str
    script_id: str
    status: str = "running"  # running | done | failed | skipped
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    rows: int | None = None
    mode: str | None = None
    errors: int | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Busy(RuntimeError):
    """A run is already going in this process."""

    def __init__(self, job: RunJob) -> None:
        super().__init__(f"a run of {job.script_id} is in progress")
        self.job = job


_lock = threading.Lock()
_jobs: dict[str, RunJob] = {}
_order: list[str] = []
_active: str | None = None


def _remember(job: RunJob) -> None:
    _jobs[job.id] = job
    _order.append(job.id)
    while len(_order) > KEEP:
        _jobs.pop(_order.pop(0), None)


def _finish(job: RunJob, report: dict[str, Any] | None, error: str | None) -> None:
    global _active
    with _lock:
        job.finished_at = datetime.now(timezone.utc).isoformat()
        s = ((report or {}).get("scripts") or {}).get(job.script_id)
        if error is not None:
            job.status, job.message = "failed", error[:500]
        elif s is None:
            job.status, job.message = "failed", "the script was not built (switched off or removed)"
        elif s.get("skipped"):
            job.status, job.message = "skipped", str(s["skipped"])
        else:
            job.rows, job.mode, job.errors = s.get("rows"), s.get("mode"), s.get("errors")
            runner_down = (s.get("error_sample") or {}).get("*")
            if runner_down:
                job.status, job.message = "failed", str(runner_down)[:500]
            else:
                job.status = "done"
                if s.get("errors"):
                    job.message = f"{s['errors']} names did not run"
        if _active == job.id:
            _active = None


def _work(job: RunJob, runner: Callable[..., dict[str, Any]]) -> None:
    try:
        report = runner(script_ids=[job.script_id], lock="skip")
    except Exception as exc:  # noqa: BLE001 — a failed run is reported on the job, never raised into the API
        logger.exception("pine run-now %s failed", job.script_id)
        _finish(job, None, f"{type(exc).__name__}: {exc}")
        return
    _finish(job, report, None)


def start(script_id: str, *, runner: Callable[..., dict[str, Any]] | None = None, background: bool = True) -> RunJob:
    """Start a run of ``script_id``; Busy when one is going in this process."""
    global _active
    with _lock:
        if _active is not None and _jobs.get(_active) and _jobs[_active].status == "running":
            raise Busy(_jobs[_active])
        job = RunJob(id=f"pr-{uuid.uuid4().hex[:12]}", script_id=script_id)
        _remember(job)
        _active = job.id
    fn = runner or build.run
    if background:
        threading.Thread(target=_work, args=(job, fn), name=f"pine-run-{script_id}", daemon=True).start()
    else:
        _work(job, fn)
    return job


def get(job_id: str) -> RunJob | None:
    with _lock:
        return _jobs.get(job_id)


def latest(script_id: str) -> RunJob | None:
    with _lock:
        for jid in reversed(_order):
            j = _jobs.get(jid)
            if j and j.script_id == script_id:
                return j
    return None


def _reset_for_tests() -> None:
    global _active
    with _lock:
        _jobs.clear()
        _order.clear()
        _active = None


__all__ = ["Busy", "RunJob", "get", "latest", "start"]
