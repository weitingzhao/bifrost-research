"""The overlay reads the option faces from the lens layer — C-A1 / C-A2.

The harness used to attach the scan table's own copy of iv_rank and vrp, which
could disagree with the page a candidate card links to. It now reads them
through ``lenses.screen``, and records the faces a symbol does not have so
judgement can say "not measured" instead of weighing fewer faces in silence.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from bifrost_research.copilot.harness.policy_schema import LoopPolicy, OptionOverlayPolicy
from bifrost_research.copilot.harness.universe import option_overlay as mod
from bifrost_research.lenses.screen import LensReading, ScreenResult, ScreenRow


def _result(rows: dict[str, dict[str, Any]]) -> ScreenResult:
    return ScreenResult(
        lenses=mod.OVERLAY_LENSES,
        universe=tuple(rows),
        rows=tuple(
            ScreenRow(
                symbol=sym,
                readings={
                    lens: LensReading(lens=lens, value=v[0], band=v[1], as_of=date(2026, 9, 4))
                    for lens, v in found.items()
                },
                missing=tuple(x for x in mod.OVERLAY_LENSES if x not in found),
                survived=True,
            )
            for sym, found in rows.items()
        ),
    )


def _policy() -> LoopPolicy:
    return LoopPolicy()


def test_a_kept_symbol_carries_its_faces_and_names_the_ones_it_lacks(monkeypatch) -> None:
    monkeypatch.setattr(
        mod.ds, "top_scan_symbols", lambda *a, **k: [{"symbol": "NVDA", "composite_score": 0.8}]
    )
    monkeypatch.setattr(
        mod,
        "screen",
        lambda conn, **k: _result(
            {"NVDA": {"iv_rank": (88.0, "hot"), "vrp": (12.0, "cold")}, "MSFT": {}}
        ),
    )

    kept, meta, step, applied = mod.apply_option_overlay(
        object(),
        symbols=["NVDA", "MSFT"],
        row_meta={"NVDA": {"sepa_score": 91.0}},
        overlay=OptionOverlayPolicy(enabled=True),
        policy=_policy(),
    )

    assert applied is True
    assert sorted(kept) == ["MSFT", "NVDA"]  # not required — nothing is dropped
    nvda = meta["NVDA"]
    assert nvda["sepa_score"] == 91.0  # the stock meta survives
    assert nvda["option_faces"]["iv_rank"] == {
        "value": 88.0,
        "band": "hot",
        "as_of": date(2026, 9, 4),
    }
    assert set(nvda["option_faces_missing"]) == {
        "iv_percentile",
        "gex_regime",
        "opex_pin",
        "terrain_regime",
    }
    # MSFT has the option side of nothing, and says so rather than being silent.
    assert meta["MSFT"]["option_faces"] == {}
    assert list(meta["MSFT"]["option_faces_missing"]) == list(mod.OVERLAY_LENSES)
    assert "option faces read for 1/2" in step.filter_summary


def test_the_gate_is_still_the_scan_composite_not_the_bands(monkeypatch) -> None:
    # NVDA is below min_composite; the lens layer says its IV rank is hot.
    monkeypatch.setattr(
        mod.ds, "top_scan_symbols", lambda *a, **k: [{"symbol": "NVDA", "composite_score": 0.2}]
    )
    monkeypatch.setattr(
        mod, "screen", lambda conn, **k: _result({"AAPL": {"iv_rank": (90.0, "hot")}})
    )

    kept, _meta, step, _applied = mod.apply_option_overlay(
        object(),
        symbols=["NVDA"],
        row_meta={},
        overlay=OptionOverlayPolicy(enabled=True, required=True, min_composite=0.5),
        policy=_policy(),
    )

    # required + below the composite floor drops it — and the fail-soft keeps
    # the stock set rather than proposing nothing.
    assert kept == ["NVDA"]
    assert "min_composite=0.5" in step.filter_summary


def test_a_lens_layer_that_will_not_answer_does_not_sink_the_funnel(monkeypatch) -> None:
    monkeypatch.setattr(
        mod.ds, "top_scan_symbols", lambda *a, **k: [{"symbol": "NVDA", "composite_score": 0.9}]
    )

    def boom(conn: Any, **k: Any) -> ScreenResult:
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(mod, "screen", boom)

    kept, meta, _step, applied = mod.apply_option_overlay(
        object(),
        symbols=["NVDA"],
        row_meta={"NVDA": {"sepa_score": 91.0}},
        overlay=OptionOverlayPolicy(enabled=True),
        policy=_policy(),
    )

    assert applied is True and kept == ["NVDA"]
    assert meta["NVDA"]["sepa_score"] == 91.0
    assert "option_faces" not in meta["NVDA"]  # unread, not "absent"


def test_a_disabled_overlay_reads_nothing(monkeypatch) -> None:
    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not query")

    monkeypatch.setattr(mod, "screen", never)
    monkeypatch.setattr(mod.ds, "top_scan_symbols", never)

    kept, meta, step, applied = mod.apply_option_overlay(
        object(),
        symbols=["NVDA"],
        row_meta={},
        overlay=OptionOverlayPolicy(enabled=False),
        policy=_policy(),
    )
    assert (kept, meta, step, applied) == (["NVDA"], {}, None, False)
