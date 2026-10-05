"""Bar-by-bar option position simulator (P2, 0.170.0).

The event backtest prices a structure twice — once in, once out. This one opens
a structure on a schedule and walks it session by session: mark every leg, take
profit, stop out, exit at a DTE floor, or settle at expiry. It answers "which
structure, which delta, which DTE, which management rule" from the two years of
``raw_market.option_daily`` the event engine never used.

Layers: ``chain`` (one symbol's chain in memory, IV/delta solved on demand),
``structures`` (leg specs), ``rules`` (config + fill/margin models), ``engine``
(the session loop and the summary).

D10 BLOCKED — historical replay only; nothing here can reach an order.
"""

from bifrost_research.engines.backtest.sim.engine import SimResult, run_sim
from bifrost_research.engines.backtest.sim.rules import SimConfig
from bifrost_research.engines.backtest.sim.structures import STRUCTURES

__all__ = ["STRUCTURES", "SimConfig", "SimResult", "run_sim"]
