"""The settleable-suggestion contract (design §2).

The one test: a settlement program can compute the P&L without anyone adding
information. An incomplete suggestion is refused here, before it is written —
the table's CHECK is the second line.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from bifrost_research.schema.suggestion_ledger_ddl import SUGGESTION_KINDS, SUGGESTION_SOURCES

LEG_KEYS = ("contract_key", "right", "side", "strike", "expiry", "ratio")


class IncompleteSuggestion(ValueError):
    """The suggestion cannot be settled as given; it is not written."""


@dataclass(frozen=True)
class Suggestion:
    as_of_session: date
    source: str
    source_ref: str
    source_version: str
    symbol: str
    kind: str
    structure: str | None = None
    legs: tuple[dict[str, Any], ...] = ()
    take_profit_pct: float | None = None
    stop_loss_mult: float | None = None
    exit_dte: int | None = None
    max_hold_days: int | None = None
    horizon_days: int | None = None
    expected_credit: float | None = None
    max_loss: float | None = None
    pop: float | None = None
    expected_pnl: float | None = None
    conviction: int | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    rationale: str | None = None
    hypothesis_id: str | None = None
    candidate_id: str | None = None
    supersedes_id: str | None = None

    # -- identity -----------------------------------------------------------------

    @property
    def issue_key(self) -> str:
        """One suggestion per (source, its config, session, symbol): reruns are no-ops."""
        return f"{self.source}:{self.source_ref}:{self.as_of_session.isoformat()}:{self.symbol.upper()}"

    @property
    def suggestion_id(self) -> str:
        return "sg_" + hashlib.sha256(self.issue_key.encode()).hexdigest()[:20]

    @property
    def inputs_hash(self) -> str:
        body = {
            "legs": list(self.legs),
            "rules": [self.take_profit_pct, self.stop_loss_mult, self.exit_dte, self.max_hold_days, self.horizon_days],
            "snapshot": self.snapshot,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    # -- validation ---------------------------------------------------------------

    def validate(self) -> "Suggestion":
        if self.source not in SUGGESTION_SOURCES:
            raise IncompleteSuggestion(f"source {self.source!r} not in {SUGGESTION_SOURCES}")
        if self.kind not in SUGGESTION_KINDS:
            raise IncompleteSuggestion(f"kind {self.kind!r} not in {SUGGESTION_KINDS}")
        if not self.symbol.strip():
            raise IncompleteSuggestion("symbol is empty")
        if not self.source_ref or not self.source_version:
            raise IncompleteSuggestion("source_ref and source_version are required")
        if self.conviction is not None and not 1 <= self.conviction <= 5:
            raise IncompleteSuggestion("conviction is 1..5")
        if self.kind == "option_structure":
            if not self.structure:
                raise IncompleteSuggestion("option_structure needs a structure")
            if not self.legs:
                raise IncompleteSuggestion("option_structure needs legs")
            for i, leg in enumerate(self.legs):
                missing = [k for k in LEG_KEYS if leg.get(k) in (None, "")]
                if missing:
                    raise IncompleteSuggestion(f"leg {i} is missing {missing}")
                if str(leg["right"]).upper() not in ("C", "P") or str(leg["side"]).lower() not in ("buy", "sell"):
                    raise IncompleteSuggestion(f"leg {i}: right={leg['right']!r} side={leg['side']!r}")
                if date.fromisoformat(str(leg["expiry"])) <= self.as_of_session:
                    raise IncompleteSuggestion(f"leg {i} expires on or before the session it was issued for")
            if self.take_profit_pct is None and self.stop_loss_mult is None and self.exit_dte is None:
                # Held to expiry is a rule, but it must be stated, not defaulted by omission.
                if self.max_hold_days is None and not self.snapshot.get("hold_to_expiry"):
                    raise IncompleteSuggestion("no management rule and hold_to_expiry not stated")
        return self

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        legs = d.pop("legs")
        snapshot = d.pop("snapshot")
        return {
            **d,
            "suggestion_id": self.suggestion_id,
            "issue_key": self.issue_key,
            "symbol": self.symbol.upper(),
            "legs_json": json.dumps(list(legs), default=str),
            "snapshot_json": json.dumps(snapshot, default=str),
            "inputs_hash": self.inputs_hash,
        }


__all__ = ["LEG_KEYS", "IncompleteSuggestion", "Suggestion"]
