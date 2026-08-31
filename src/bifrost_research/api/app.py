"""FastAPI application factory for Bifrost Research API (port 8795)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bifrost_research import __version__
from bifrost_research.api.agent_persona import router as agent_persona_router
from bifrost_research.api.agents import agents_router, drafts_router
from bifrost_research.api.alerts import router as alerts_router
from bifrost_research.api.backtest_event import router as backtest_event_router
from bifrost_research.api.canonical_pnl import router as canonical_pnl_router
from bifrost_research.api.candidates import router as candidates_router
from bifrost_research.api.copilot import router as copilot_router
from bifrost_research.api.copilot_sessions import router as copilot_sessions_router
from bifrost_research.api.elementary import router as elementary_router
from bifrost_research.api.exhibit import router as exhibit_router
from bifrost_research.api.harness import router as harness_router
from bifrost_research.api.health import router as health_router
from bifrost_research.api.hypothesis import router as hypothesis_router
from bifrost_research.api.opex_cycle import router as opex_cycle_router
from bifrost_research.api.options import router as options_router
from bifrost_research.api.order_intents import router as order_intents_router
from bifrost_research.api.playbook import router as playbook_router
from bifrost_research.api.research_engines import router as research_engines_router
from bifrost_research.api.scan import router as scan_router
from bifrost_research.api.signal_decay import router as signal_decay_router
from bifrost_research.api.sepa import router as sepa_router
from bifrost_research.api.signal_health import router as signal_health_router
from bifrost_research.api.orchestration import router as orchestration_router
from bifrost_research.api.similar_regime import router as similar_regime_router
from bifrost_research.api.vol_surface import router as vol_surface_router
from bifrost_research.api.vrp import router as vrp_router
from bifrost_research.api.wave4 import router as wave4_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from bifrost_research.api.health import run_startup_schema_guard

    run_startup_schema_guard()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Bifrost Research API",
        version=__version__,
        description=(
            "Bifrost Research Engine API (port 8795). "
            "SEPA analytics, options features.* metrics, Wave 3 engines "
            "(momentum / GEX / IV surface / order flow), Wave 4 AI Intelligence "
            "(terrain / forecast / event radar / backtest), and Elementary report files."
        ),
        lifespan=_lifespan,
    )
    app.include_router(health_router)
    app.include_router(sepa_router)
    app.include_router(options_router)
    app.include_router(elementary_router)
    app.include_router(research_engines_router)
    app.include_router(wave4_router)
    app.include_router(hypothesis_router)
    app.include_router(candidates_router)
    app.include_router(harness_router)
    app.include_router(order_intents_router)
    app.include_router(vrp_router)
    app.include_router(canonical_pnl_router)
    app.include_router(signal_health_router)
    app.include_router(orchestration_router)
    app.include_router(similar_regime_router)
    app.include_router(scan_router)
    app.include_router(signal_decay_router)
    app.include_router(alerts_router)
    app.include_router(exhibit_router)
    app.include_router(vol_surface_router)
    app.include_router(opex_cycle_router)
    app.include_router(backtest_event_router)
    app.include_router(copilot_router)
    app.include_router(copilot_sessions_router)
    app.include_router(playbook_router)
    app.include_router(agent_persona_router)
    app.include_router(agents_router)
    app.include_router(drafts_router)
    return app


app = create_app()
