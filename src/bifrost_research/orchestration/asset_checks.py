"""Every asset has an output check (TD-92).

An engine asset used to wrap whatever its runner returned into materialization
metadata, so a run that wrote nothing, failed for most names, or skipped looked
exactly like a good one: the Ops schedule view and ``failure_alerts`` saw SUCCESS.
On 2026-10-06 gex failed 42 of 1,408 symbol-days, flow 41 and surface 41, and
option_pinned wrote 0 rows (TD-89) — all green, visible only by reading metadata.

Each asset now declares one non-blocking check, ``output_ok``, judged from the
result its runner returned (``OutputSpec`` says which fields mean what):

- ERROR (alerted through ``failure_alerts.bifrost_asset_check_alert``):
  zero rows where the engine always writes rows; a failure share above
  ``FAILURE_SHARE_CEILING`` (normal is ~3%, TD-89-class breakage is 90%+);
  skipped twice running; an ``error`` / ``ok: false`` in the result.
- WARN (visible on the asset in Dagster, not alerted): one skip, with its reason;
  rows below ``TRAILING_FLOOR`` of the trailing median; a softer failure share
  nobody has measured a normal for yet; names past onboarding that failed every
  session of the window (NVR / GRML in TD-92).

Non-blocking on purpose: a check that fails leaves the run green and runs the
downstream assets, so a thin night cannot turn research_trading_day red; the
alert is what makes it seen. Assets that raise on their own (gex_intraday,
option_pinned's shape errors, the gate) still fail the run as before.

Note: do not use ``from __future__ import annotations`` — Dagster needs live types.
"""

import logging
import statistics
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from dagster import (
    AssetCheckKey,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetCheckSpec,
    AssetExecutionContext,
    AssetKey,
    MaterializeResult,
)

logger = logging.getLogger(__name__)

OUTPUT_CHECK = "output_ok"

#: Failed share of symbol-days above which an engine is broken, not thin. Measured
#: 2026-10-06: gex 42/1,408 (3.0%), flow and surface the same order; TD-89-class
#: breakage (gex_intraday 646-669/669 for three weeks) sits at 97-100%. A week of
#: onboarding ~20 new names a night stays under 10%.
FAILURE_SHARE_CEILING = 0.25
#: Rows below this share of the trailing median are a partial night (WARN).
TRAILING_FLOOR = 0.5
#: Materializations the trailing median reads; fewer than three judges nothing.
TRAILING_RUNS = 10
TRAILING_MIN_RUNS = 3

#: Metadata keys every judged materialization carries, so the next run reads its
#: own history without knowing each runner's field names.
ROWS_KEY = "output_rows"
SKIPPED_KEY = "output_skipped"

ERROR = AssetCheckSeverity.ERROR
WARN = AssetCheckSeverity.WARN


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def field(name: str) -> Callable[[Mapping[str, Any]], int | None]:
    """Rows are the top-level integer ``name``."""

    def read(result: Mapping[str, Any]) -> int | None:
        return _int(result.get(name))

    read.__name__ = f"field_{name}"
    return read


@dataclass(frozen=True)
class OutputSpec:
    """How to read one runner's result.

    ``rows`` extracts the rows written (None: not judged on rows). ``expect_rows``
    makes zero an ERROR: only for engines that rewrite a window of sessions every
    night, so zero cannot be a quiet day. ``failed`` / ``soft_failed`` name
    (failed, ok) counter pairs, judged against FAILURE_SHARE_CEILING at ERROR and
    at WARN respectively. ``trailing`` judges rows against
    the trailing median. ``error_severity`` applies to ``error`` / ``ok: false``.
    """

    rows: Callable[[Mapping[str, Any]], int | None] | None = None
    expect_rows: bool = False
    failed: tuple[tuple[str, str], ...] = ()
    soft_failed: tuple[tuple[str, str], ...] = ()
    trailing: bool = True
    error_severity: AssetCheckSeverity = ERROR
    extra: Callable[[Mapping[str, Any]], list[tuple[AssetCheckSeverity, str]]] | None = None


GENERIC = OutputSpec()


def skip_reason(result: Mapping[str, Any]) -> str | None:
    """A runner that did no work says so with ``skipped: True`` or ``mode: skipped``.

    ``skipped`` as an integer is a count of skipped names (momentum, vrp), not a skip.
    """
    skipped = result.get("skipped")
    if (isinstance(skipped, bool) and skipped) or result.get("mode") == "skipped":
        return str(result.get("reason") or "no reason given")
    return None


def _share(result: Mapping[str, Any], pair: tuple[str, str]) -> tuple[int, int] | None:
    failed, ok = _int(result.get(pair[0])), _int(result.get(pair[1]))
    if failed is None or ok is None:
        return None
    return failed, failed + ok


def judge_output(
    result: Mapping[str, Any],
    spec: OutputSpec,
    *,
    history: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[tuple[AssetCheckSeverity, str]], dict[str, Any]]:
    """Findings (severity, text) and check metadata for one runner result.

    ``history`` is the previous materializations' metadata, newest first, as plain
    values (``ROWS_KEY`` / ``SKIPPED_KEY``).
    """
    findings: list[tuple[AssetCheckSeverity, str]] = []
    meta: dict[str, Any] = {}

    reason = skip_reason(result)
    if reason is not None:
        twice = bool(history) and bool(history[0].get(SKIPPED_KEY))
        findings.append((ERROR if twice else WARN, f"skipped{' twice running' if twice else ''}: {reason}"))
        meta["skip_reason"] = reason

    error = result.get("error")
    if error:
        findings.append((spec.error_severity, f"error: {str(error)[:300]}"))
    if result.get("ok") is False:
        findings.append((spec.error_severity, "result says ok=false"))

    rows = spec.rows(result) if spec.rows is not None else None
    if rows is not None:
        meta["rows"] = rows
        if reason is None and spec.expect_rows and rows <= 0:
            findings.append((ERROR, "wrote 0 rows; this engine rewrites a window every run"))
        past = [v for v in (_int(h.get(ROWS_KEY)) for h in history) if v is not None]
        if spec.trailing and reason is None and len(past) >= TRAILING_MIN_RUNS:
            median = statistics.median(past)
            meta["trailing_median_rows"] = float(median)
            if rows > 0 and median > 0 and rows < TRAILING_FLOOR * median:
                findings.append(
                    (WARN, f"rows {rows} < {TRAILING_FLOOR:.0%} of the trailing median {median:g}")
                )

    judged = [(p, ERROR) for p in spec.failed] + [(p, WARN) for p in spec.soft_failed]
    for pair, severity in judged:
        counts = _share(result, pair)
        if counts is None or counts[1] == 0:
            continue
        failed, total = counts
        share = failed / total
        meta[f"{pair[0]}_share"] = round(share, 4)
        if share > FAILURE_SHARE_CEILING:
            sample = result.get("failures_by_reason") or result.get("failures") or ""
            findings.append(
                (
                    severity,
                    f"{pair[0]} {failed}/{total} ({share:.0%}) > {FAILURE_SHARE_CEILING:.0%}"
                    + (f" — {str(sample)[:200]}" if sample else ""),
                )
            )

    persistent = result.get("failed_every_session_past_onboarding")
    if persistent:
        findings.append(
            (WARN, f"failed every session of the window, past onboarding: {', '.join(map(str, persistent))[:300]}")
        )

    if spec.extra is not None:
        findings.extend(spec.extra(result))
    return findings, meta


def check_result(
    result: Mapping[str, Any],
    spec: OutputSpec,
    *,
    history: Sequence[Mapping[str, Any]] = (),
) -> AssetCheckResult:
    findings, meta = judge_output(result, spec, history=history)
    severity = ERROR if any(s == ERROR for s, _ in findings) else WARN
    meta["findings"] = "; ".join(text for _, text in findings) or "none"
    return AssetCheckResult(
        check_name=OUTPUT_CHECK,
        passed=not findings,
        severity=severity,
        metadata=meta,
    )


def output_check_specs(key: AssetKey) -> list[AssetCheckSpec]:
    return [
        AssetCheckSpec(
            OUTPUT_CHECK,
            asset=key,
            blocking=False,
            description=(
                "TD-92: the runner's own result judged — zero rows where rows are "
                "expected, failure share, skips (with reason), trailing-median rows."
            ),
        )
    ]


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def _history(context: AssetExecutionContext, key: AssetKey) -> list[dict[str, Any]]:
    """The previous materializations' judged fields, newest first ([] if unreadable)."""
    try:
        records = context.instance.fetch_materializations(key, limit=TRAILING_RUNS).records
    except Exception as exc:  # noqa: BLE001 — history only sharpens the check
        logger.warning("output check: history of %s unreadable: %s", key.to_user_string(), exc)
        return []
    out: list[dict[str, Any]] = []
    for record in records:
        mat = record.asset_materialization
        if mat is None:
            continue
        md = mat.metadata or {}
        out.append({ROWS_KEY: _plain(md.get(ROWS_KEY)), SKIPPED_KEY: _plain(md.get(SKIPPED_KEY))})
    return out


def judged_materialization(
    context: AssetExecutionContext,
    result: Mapping[str, Any],
    metadata: dict[str, Any],
    spec: OutputSpec = GENERIC,
) -> MaterializeResult:
    """``MaterializeResult`` carrying the ``output_ok`` check when it is selected."""
    key = context.asset_key
    rows = spec.rows(result) if spec.rows is not None else None
    md = dict(metadata)
    if rows is not None:
        md[ROWS_KEY] = rows
    md[SKIPPED_KEY] = skip_reason(result) is not None
    checks: list[AssetCheckResult] = []
    if AssetCheckKey(key, OUTPUT_CHECK) in context.selected_asset_check_keys:
        verdict = check_result(result, spec, history=_history(context, key))
        if not verdict.passed:
            context.log.warning(
                "%s output check %s: %s",
                key.to_user_string(),
                verdict.severity.value,
                _plain(verdict.metadata.get("findings")),
            )
        checks.append(verdict)
    return MaterializeResult(metadata=md, check_results=checks)


# ---------------------------------------------------------------------------
# Opt-outs: an asset without an output check must say why (the coverage test
# tests/orchestration/test_asset_check_coverage.py fails otherwise).
# ---------------------------------------------------------------------------

_ENQUEUE_ONLY = (
    "enqueue only: the asset POSTs a slot to the Market Data Plugin and raises on "
    "a refused enqueue; the work and its outcome belong to the plugin's workers, "
    "judged by its doctor and by husbandry_gate"
)

OUTPUT_CHECK_OPT_OUT: dict[str, str] = {
    "engines/backtest": (
        "scaffold: runners.run_backtest aggregates an empty list and writes nothing; "
        "forecast settlement is engines/settlement, which is checked"
    ),
    "engines/event_radar": (
        "file ingest where an empty input directory is a legitimate idle; whether "
        "the input mount exists is TD-100's check"
    ),
    "engines/event_radar_cron": (
        "SEC 8-K ingest every 30 minutes: most ticks find no new filing (they land once "
        "a day), so 0 rows is normal; a failure raises, and BifrostEventRadarSecBacklog "
        "watches filings vs rows (TD-100)"
    ),
    "maintenance/terrain_backfill": "manual run; the operator who launched it reads its report",
    "maintenance/event_radar_purge": "manual one-off (dry run by default); the operator reads its counts",
    "maintenance/signal_backfill": "manual deep recompute; the operator reads its result",
    "batch/market_eod": _ENQUEUE_ONLY,
    "batch/flex_trades": (
        "enqueue only: the Flex plugin's worker does the work; its outcome is judged "
        "by husbandry_gate (freshness-kpis), which raises"
    ),
    "batch/flex_transactions": (
        "enqueue only: the Flex plugin's worker does the work; its outcome is judged "
        "by husbandry_gate (freshness-kpis), which raises"
    ),
    "batch/husbandry_gate": "the gate is itself a check: it raises (fails closed) instead of reporting",
    "batch/market/market_self_heal": (
        "doctor -> heal -> recheck: it raises when the session is still critical after the heal"
    ),
}
#: Market slot assets (market_slot_schedules) are all enqueue-only; matched by prefix.
OUTPUT_CHECK_OPT_OUT_PREFIX: dict[str, str] = {
    "batch/market/": _ENQUEUE_ONLY,
}


def opt_out_reason(key: AssetKey) -> str | None:
    name = key.to_user_string()
    if name in OUTPUT_CHECK_OPT_OUT:
        return OUTPUT_CHECK_OPT_OUT[name]
    for prefix, reason in OUTPUT_CHECK_OPT_OUT_PREFIX.items():
        if name.startswith(prefix):
            return reason
    return None
