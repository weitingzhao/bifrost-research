"""FastAPI application factory for Bifrost Research API (port 8795)."""

from __future__ import annotations

from fastapi import FastAPI

from bifrost_research import __version__
from bifrost_research.api.elementary import router as elementary_router
from bifrost_research.api.health import router as health_router
from bifrost_research.api.options import router as options_router
from bifrost_research.api.research_engines import router as research_engines_router
from bifrost_research.api.sepa import router as sepa_router
from bifrost_research.api.wave4 import router as wave4_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Bifrost Research API",
        version=__version__,
        description=(
            "Bifrost Research Engine API (port 8795). "
            "SEPA analytics, options market_analytics, Wave 3 engines "
            "(momentum / GEX / IV surface / order flow), Wave 4 AI Intelligence "
            "(terrain / forecast / event radar / backtest), and Elementary status."
        ),
    )
    app.include_router(health_router)
    app.include_router(sepa_router)
    app.include_router(options_router)
    app.include_router(elementary_router)
    app.include_router(research_engines_router)
    app.include_router(wave4_router)
    return app


app = create_app()
