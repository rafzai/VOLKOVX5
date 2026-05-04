"""
VOLKOVX_V4 — Production Polymarket trading bot

This package ships TWO independent trading systems that share a repo
but not state:

  1.  ARBITRAGE ENGINE (root-level modules)
      Detects and executes guaranteed-profit arbitrage on any binary
      Polymarket market.  Files:
        bregman_projection.py    — math + arbitrage detection
        frank_wolfe_solver.py    — multi-market capital allocator
        engine_v2.py             — atomic-execution engine

      Usage:
        from VOLKOVX_V4 import (
            VolkovxArbitrageEngineV2, EngineConfig, FeeSchedule,
            BregmanProjectionEngine, FrankWolfeSolver,
        )

  2.  BTC 5-MIN SNIPER (volkovx/ subpackage)
      Specialised bot for the Polymarket "Bitcoin Up or Down 5-min"
      market with 7 filters (entry zone, beat distance, liquidations,
      CVD, odds sanity, gocek cooldown, feed freshness).

      Usage (CLI):
          python -m volkovx                 # from this directory

See AUDIT_REPORT.md for code review notes and DEPLOYMENT_GUIDE.md for
the DRY → CANARY → LIVE rollout runbook.
"""

__version__ = "4.1.0"
__author__  = "VOLKOVX Team"

# ── Arbitrage engine — primary public API ─────────────────────────────
from VOLKOVX_V4.bregman_projection import (
    ArbitrageOpportunity,
    ArbitrageType,
    ArbDirection,
    BregmanProjectionEngine,
    FeeCalculator,
    FeeSchedule,
)
from VOLKOVX_V4.frank_wolfe_solver import (
    FrankWolfeSolver,
    MarketAllocationInput,
    OptimizationResult,
)
from VOLKOVX_V4.engine_v2 import (
    EngineConfig,
    Leg,
    PerformanceMetrics,
    TradeRecord,
    TradeStatus,
    VolkovxArbitrageEngineV2,
)

__all__ = [
    "VolkovxArbitrageEngineV2",
    "EngineConfig",
    "Leg",
    "TradeRecord",
    "TradeStatus",
    "PerformanceMetrics",
    "BregmanProjectionEngine",
    "ArbitrageOpportunity",
    "ArbitrageType",
    "ArbDirection",
    "FeeSchedule",
    "FeeCalculator",
    "FrankWolfeSolver",
    "MarketAllocationInput",
    "OptimizationResult",
]
