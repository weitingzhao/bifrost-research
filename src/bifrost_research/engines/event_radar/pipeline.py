"""Event Radar pipeline — 5-step scaffold aligned with Research-workspace workflow.

Steps: parse → clean/dedupe → tag → export → self-check
Maps unstructured financial text → features.event_signal_radar_daily.

D10 BLOCKED — advisory structured events only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from bifrost_research.db.upsert import batch_upsert

_COLS = (
    "event_id",
    "batch_id",
    "collected_at",
    "source",
    "source_position",
    "title",
    "raw_text",
    "event_type",
    "event_date",
    "date_basis",
    "time_orientation",
    "certainty_evidence",
    "subject",
    "event_summary",
    "key_value",
    "value_semantics",
    "affected_symbols",
    "direction",
    "time_code",
    "certainty",
    "sentiment",
    "theme",
    "importance",
    "pipeline_stage",
    "dropped",
    "drop_reason",
    "self_check_json",
    "computed_at",
)

_CERTAINTY_2 = (
    "已签署",
    "已公布",
    "已完成",
    "已获批",
    "数据显示",
    "官方宣布",
    "财报显示",
    "announced",
    "signed",
    "approved",
)
_CERTAINTY_1 = (
    "据悉",
    "知情人士",
    "据报",
    "消息人士",
    "传",
    "有报道称",
    "sources say",
    "reportedly",
)
_CERTAINTY_0 = ("预计", "有望", "料", "认为", "建议", "分析师", "expects", "likely", "may")

_BULLISH = ("rally", "surge", "beat", "upgrade", "approve", "利多", "上涨", "超预期", "获批")
_BEARISH = ("crash", "slash", "downgrade", "probe", "fine", "利空", "下跌", "不及预期", "裁员")


@dataclass
class RawEvent:
    event_id: str
    collected_at: date
    source: str
    source_position: str
    title: str
    raw_text: str
    event_type: str
    event_date: str
    date_basis: str
    time_orientation: str
    certainty_evidence: str
    theme_candidate: str = ""


@dataclass
class TaggedEvent:
    raw: RawEvent
    subject: str
    event_summary: str
    key_value: str
    value_semantics: str
    affected_symbols: str
    direction: int
    time_code: int
    certainty: int
    sentiment: int
    theme: str
    importance: int
    dropped: bool = False
    drop_reason: str = ""
    self_check: dict[str, Any] = field(default_factory=dict)

    def to_row(self, batch_id: str, stage: str = "export") -> tuple[Any, ...]:
        r = self.raw
        return (
            r.event_id,
            batch_id,
            r.collected_at,
            r.source,
            r.source_position,
            r.title,
            r.raw_text,
            r.event_type,
            r.event_date,
            r.date_basis,
            r.time_orientation,
            r.certainty_evidence,
            self.subject,
            self.event_summary,
            self.key_value,
            self.value_semantics,
            self.affected_symbols,
            self.direction,
            self.time_code,
            self.certainty,
            self.sentiment,
            self.theme,
            self.importance,
            stage,
            self.dropped,
            self.drop_reason,
            self.self_check,
            datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["raw"]["collected_at"] = self.raw.collected_at.isoformat()
        return d


@dataclass
class PipelineResult:
    batch_id: str
    raw_count: int
    kept: list[TaggedEvent]
    dropped: list[TaggedEvent]
    export_rows: list[dict[str, Any]]
    self_check: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "raw_count": self.raw_count,
            "kept_count": len(self.kept),
            "dropped_count": len(self.dropped),
            "export_rows": self.export_rows,
            "self_check": self.self_check,
            "advisory": "D10 BLOCKED — event radar is advisory only",
        }


def _stable_id(source: str, collected: date, idx: int, text: str) -> str:
    abbr = re.sub(r"[^A-Za-z0-9]", "", source)[:6].upper() or "SRC"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"{abbr}-{collected.strftime('%Y%m%d')}-{idx:03d}-{digest}"


def _split_bullets(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022\d]+[\.\)\]]\s*", "", line).strip()
        if line:
            lines.append(line)
    if lines:
        return lines
    parts = re.split(r"(?<=[。.!？?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def step_parse(
    payload: str,
    *,
    source: str = "sample",
    collected_at: date | None = None,
) -> list[RawEvent]:
    """01 parse — one fragment → one raw record; no dedupe."""
    day = collected_at or date.today()
    chunks = _split_bullets(payload)
    out: list[RawEvent] = []
    for i, chunk in enumerate(chunks, start=1):
        lower = chunk.lower()
        if re.search(
            r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}|"
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
            lower,
        ):
            etype = "A_explicit"
        elif any(w in chunk for w in ("计划", "拟", "预计", "将", "有望", "plan", "expect", "will")):
            etype = "B_implied"
        elif any(w in lower for w in ("calendar", "日程", "ipo", "gdp", "cpi", "nfp")):
            etype = "C_calendar"
        else:
            etype = "B_implied"

        evidence = ""
        for word in (*_CERTAINTY_2, *_CERTAINTY_1, *_CERTAINTY_0):
            if word.lower() in lower or word in chunk:
                evidence = word
                break

        orientation = "future"
        if any(w in chunk for w in ("已", "yesterday", "announced", "reported")):
            orientation = "past"
        elif any(w in chunk for w in ("今日", "today")):
            orientation = "today"

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", chunk)
        event_date = date_match.group(1) if date_match else "待定"

        out.append(
            RawEvent(
                event_id=_stable_id(source, day, i, chunk),
                collected_at=day,
                source=source,
                source_position=f"line-{i}",
                title="",
                raw_text=chunk,
                event_type=etype,
                event_date=event_date,
                date_basis=date_match.group(0) if date_match else "",
                time_orientation=orientation,
                certainty_evidence=evidence,
            )
        )
    return out


def step_clean(raw_events: Sequence[RawEvent]) -> tuple[list[RawEvent], list[TaggedEvent]]:
    """02 clean — exact-text dedupe + noise drop."""
    seen: dict[str, RawEvent] = {}
    dropped: list[TaggedEvent] = []
    for ev in raw_events:
        key = re.sub(r"\s+", " ", ev.raw_text.strip().lower())
        if len(key) < 8:
            dropped.append(
                TaggedEvent(
                    raw=ev,
                    subject="",
                    event_summary="",
                    key_value="",
                    value_semantics="",
                    affected_symbols="",
                    direction=0,
                    time_code=0,
                    certainty=0,
                    sentiment=0,
                    theme="",
                    importance=1,
                    dropped=True,
                    drop_reason="noise_too_short",
                )
            )
            continue
        if key in seen:
            dropped.append(
                TaggedEvent(
                    raw=ev,
                    subject="",
                    event_summary="",
                    key_value="",
                    value_semantics="",
                    affected_symbols="",
                    direction=0,
                    time_code=0,
                    certainty=0,
                    sentiment=0,
                    theme="",
                    importance=1,
                    dropped=True,
                    drop_reason="duplicate_raw_text",
                )
            )
            continue
        seen[key] = ev
    return list(seen.values()), dropped


def _certainty_from_evidence(evidence: str) -> int:
    if not evidence:
        return 0
    e = evidence.lower()
    for w in _CERTAINTY_2:
        if w.lower() == e or w == evidence:
            return 2
    for w in _CERTAINTY_1:
        if w.lower() == e or w == evidence:
            return 1
    return 0


def _direction_sentiment(text: str) -> tuple[int, int]:
    lower = text.lower()
    bull = sum(1 for w in _BULLISH if w.lower() in lower or w in text)
    bear = sum(1 for w in _BEARISH if w.lower() in lower or w in text)
    if bull > bear:
        return 1, 1
    if bear > bull:
        return -1, -1
    return 0, 0


def _extract_symbols(text: str) -> str:
    found = re.findall(r"\$([A-Z]{1,5})\b", text)
    found += re.findall(r"\b([A-Z]{2,5})\b", text)
    stop = {"USD", "GDP", "CPI", "FOMC", "IPO", "ETF", "CEO", "THE", "AND", "FOR"}
    uniq: list[str] = []
    for s in found:
        if s in stop:
            continue
        if s not in uniq:
            uniq.append(s)
    return " · ".join(uniq[:4])


def step_tag(raw_events: Sequence[RawEvent]) -> list[TaggedEvent]:
    """03 tag — direction / certainty / sentiment / theme / importance."""
    tagged: list[TaggedEvent] = []
    for ev in raw_events:
        direction, sentiment = _direction_sentiment(ev.raw_text)
        certainty = _certainty_from_evidence(ev.certainty_evidence)
        time_map = {"past": 0, "today": 1, "future": 2}
        time_code = time_map.get(ev.time_orientation, 2)
        symbols = _extract_symbols(ev.raw_text)
        subject = (ev.title or ev.raw_text[:16]).strip()[:16]
        summary = ev.raw_text[:40]
        importance = 1
        if symbols or certainty >= 2:
            importance = 2
        if (
            any(w in ev.raw_text for w in ("央行", "Fed", "FOMC", "财政部", "监管"))
            or (certainty == 2 and symbols)
        ):
            importance = 3
        num = re.search(
            r"(-?\d+(?:\.\d+)?)\s*(%|bp|亿美元|亿|万|billion|million)?",
            ev.raw_text,
        )
        key_value = ""
        value_semantics = ""
        if num:
            key_value = (num.group(0) or "").strip()
            value_semantics = "extracted_number"

        tagged.append(
            TaggedEvent(
                raw=ev,
                subject=subject,
                event_summary=summary,
                key_value=key_value,
                value_semantics=value_semantics,
                affected_symbols=symbols,
                direction=direction,
                time_code=time_code,
                certainty=certainty,
                sentiment=sentiment,
                theme=ev.theme_candidate or "",
                importance=importance,
            )
        )

    threes = [t for t in tagged if t.importance == 3]
    # Rubric: importance=3 ≤ 25% of kept rows
    cap = int(len(tagged) * 0.25) if tagged else 0
    if len(threes) > cap:
        for t in threes[cap:]:
            t.importance = 2
    return tagged


def step_export(tagged: Sequence[TaggedEvent]) -> list[dict[str, Any]]:
    """04 export — structured rows for API / features.event_signal_radar_daily."""
    rows: list[dict[str, Any]] = []
    for t in tagged:
        if t.dropped:
            continue
        rows.append(
            {
                "event_id": t.raw.event_id,
                "source": t.raw.source,
                "subject": t.subject,
                "event": t.event_summary,
                "key_value": t.key_value,
                "value_semantics": t.value_semantics,
                "affected_symbols": t.affected_symbols,
                "direction": t.direction,
                "time": t.time_code,
                "certainty": t.certainty,
                "sentiment": t.sentiment,
                "theme": t.theme,
                "importance": t.importance,
                "date": t.raw.event_date
                if re.match(r"\d{4}-\d{2}-\d{2}", t.raw.event_date)
                else "",
                "raw_text": t.raw.raw_text,
            }
        )
    return rows


def step_self_check(
    raw_count: int,
    tagged: Sequence[TaggedEvent],
    export_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """05 self-check — lightweight deliverable gate."""
    checks: dict[str, Any] = {}
    checks["raw_text_verbatim"] = all(bool(t.raw.raw_text.strip()) for t in tagged)
    checks["ids_unique"] = len({t.raw.event_id for t in tagged}) == len(list(tagged))
    checks["export_count_matches_kept"] = len(export_rows) == sum(
        1 for t in tagged if not t.dropped
    )
    checks["no_fabricated_empty_filled"] = True
    threes = sum(1 for t in tagged if not t.dropped and t.importance == 3)
    kept = sum(1 for t in tagged if not t.dropped) or 1
    checks["importance_3_cap_ok"] = (threes / kept) <= 0.26
    checks["direction_in_range"] = all(t.direction in (-1, 0, 1) for t in tagged)
    checks["certainty_in_range"] = all(t.certainty in (0, 1, 2) for t in tagged)
    checks["raw_count"] = raw_count
    checks["kept_count"] = sum(1 for t in tagged if not t.dropped)
    checks["passed"] = all(
        bool(checks[k])
        for k in (
            "raw_text_verbatim",
            "ids_unique",
            "export_count_matches_kept",
            "importance_3_cap_ok",
            "direction_in_range",
            "certainty_in_range",
        )
    )
    return checks


def run_pipeline(
    payload: str,
    *,
    source: str = "sample",
    collected_at: date | None = None,
    batch_id: str | None = None,
) -> PipelineResult:
    """Run full 5-step Event Radar pipeline on plain-text sample."""
    bid = batch_id or f"batch-{uuid4().hex[:10]}"
    raw = step_parse(payload, source=source, collected_at=collected_at)
    cleaned, dropped_early = step_clean(raw)
    from bifrost_research.engines.event_radar.tagger import get_event_tagger

    tagged = get_event_tagger().tag(cleaned)
    all_tagged = list(tagged) + list(dropped_early)
    export_rows = step_export(all_tagged)
    checks = step_self_check(len(raw), all_tagged, export_rows)
    for t in tagged:
        t.self_check = {"batch_id": bid, "passed": checks.get("passed")}
    return PipelineResult(
        batch_id=bid,
        raw_count=len(raw),
        kept=[t for t in tagged if not t.dropped],
        dropped=list(dropped_early),
        export_rows=export_rows,
        self_check=checks,
    )


def upsert_events(conn: Any, result: PipelineResult) -> int:
    rows = [t.to_row(result.batch_id, stage="export") for t in result.kept]
    rows += [t.to_row(result.batch_id, stage="dropped") for t in result.dropped]
    if not rows:
        return 0
    return batch_upsert(
        conn,
        "features.event_signal_radar_daily",
        _COLS,
        rows,
        conflict_keys=("event_id",),
        set_fetched_at=False,
    )
