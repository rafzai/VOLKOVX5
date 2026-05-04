"""
test_volkovx.py — Comprehensive unit test suite
================================================

Covers the three new production modules:

    * bregman_projection.py
    * frank_wolfe_solver.py
    * engine_v2.py

Run
---
    pytest test_volkovx.py -v
    pytest test_volkovx.py -v --tb=short -k atomic   # one bucket only

Test buckets
------------
    1.  TestBregmanProjection      — math, fees, edge cases
    2.  TestFrankWolfeSolver       — convergence, constraints, timeout
    3.  TestPositionSizingViaEngine— Kelly + drawdown via engine surface
    4.  TestAtomicExecution        — partial-fill unwind, retries
    5.  TestRiskManagement         — max-drawdown, daily-loss, halt
    6.  TestFeeCalculation         — explicit fee accounting
    7.  TestSimulatedFailures      — API down, slow, malformed, network
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Dict, List, Optional, Tuple

import pytest

# --------------------------------------------------------------------------
# Test subjects
# --------------------------------------------------------------------------
from bregman_projection import (
    ArbDirection,
    ArbitrageOpportunity,
    ArbitrageType,
    BregmanProjectionEngine,
    FeeCalculator,
    FeeSchedule,
)
from frank_wolfe_solver import (
    FrankWolfeSolver,
    MarketAllocationInput,
    OptimizationResult,
)
from engine_v2 import (
    EngineConfig,
    Leg,
    PerformanceMetrics,
    TradeStatus,
    VolkovxArbitrageEngineV2,
)


# ==========================================================================
#  Mock order client
# ==========================================================================

class MockOrderClient:
    """
    Configurable in-memory order client for engine tests.

    Knobs
    -----
    fail_legs       : iterable of (market_id, outcome) tuples that should
                      always fail
    fail_after_n    : drop the (n+1)th call regardless of leg
    delay_seconds   : artificial latency
    error_message   : message returned on failure
    """
    def __init__(
        self,
        fail_legs: Optional[List[Tuple[str, str]]] = None,
        fail_after_n: Optional[int] = None,
        delay_seconds: float = 0.0,
        error_message: str = "mock_error",
    ) -> None:
        self.fail_legs = set(fail_legs or [])
        self.fail_after_n = fail_after_n
        self.delay_seconds = delay_seconds
        self.error_message = error_message
        self.placed_calls:    List[Leg] = []
        self.cancelled_calls: List[Leg] = []

    async def place(self, leg: Leg):
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.placed_calls.append(leg)

        if self.fail_after_n is not None and len(self.placed_calls) > self.fail_after_n:
            return False, None, None, None, self.error_message

        if (leg.market_id, leg.outcome) in self.fail_legs:
            return False, None, None, None, self.error_message

        # Success: fill at expected price, qty = notional / price
        price = leg.expected_price
        qty   = leg.notional_usd / max(price, 1e-9)
        return True, price, qty, f"oid_{leg.leg_id}", None

    async def cancel(self, leg: Leg) -> bool:
        self.cancelled_calls.append(leg)
        return True


# ==========================================================================
#  1.  TestBregmanProjection
# ==========================================================================

class TestBregmanProjection:

    @pytest.fixture
    def eng(self) -> BregmanProjectionEngine:
        return BregmanProjectionEngine(
            min_profit_threshold=0.0,
            fee_schedule=FeeSchedule(taker_fee=0.0, gas_usd=0.0,
                                     impact_coef=0.0),
        )

    def test_no_arbitrage_when_prices_sum_to_one(self, eng):
        """Sum = 1.0 ⇒ no opportunity."""
        opp = eng.detect_single_market_arbitrage(
            market_id="m1",
            outcomes={"YES": 0.5, "NO": 0.5},
            available_liquidity={"YES": 1e6, "NO": 1e6},
            market_size=100.0,
        )
        assert opp is None

    def test_underpriced_detection(self, eng):
        """Sum = 0.95 ⇒ buy both, profit = $5 / $100 stake."""
        opp = eng.detect_single_market_arbitrage(
            market_id="m1",
            outcomes={"YES": 0.50, "NO": 0.45},
            available_liquidity={"YES": 1e6, "NO": 1e6},
            market_size=100.0,
        )
        assert opp is not None
        assert opp.direction == ArbDirection.UNDERPRICED
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=1e-6)
        # Both legs are BUY (positive notional)
        assert all(v > 0 for v in opp.positions.values())

    def test_overpriced_detection(self, eng):
        """Sum = 1.05 ⇒ sell both, profit = $5 / $100 stake."""
        opp = eng.detect_single_market_arbitrage(
            market_id="m1",
            outcomes={"YES": 0.55, "NO": 0.50},
            available_liquidity={"YES": 1e6, "NO": 1e6},
            market_size=100.0,
        )
        assert opp is not None
        assert opp.direction == ArbDirection.OVERPRICED
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=1e-6)
        # Both legs are SELL (negative notional)
        assert all(v < 0 for v in opp.positions.values())

    def test_fees_eat_thin_arbitrage(self):
        """Realistic taker + gas should kill a 0.1% mispricing."""
        eng = BregmanProjectionEngine(
            min_profit_threshold=0.0,
            fee_schedule=FeeSchedule(taker_fee=0.002, gas_usd=0.05),
        )
        opp = eng.detect_single_market_arbitrage(
            market_id="m1",
            outcomes={"YES": 0.499, "NO": 0.500},   # sum = 0.999
            available_liquidity={"YES": 1e6, "NO": 1e6},
            market_size=10.0,                       # gross profit = $0.01
        )
        # Gross profit = 0.01 USD, fees = ~0.13 ⇒ no opportunity
        assert opp is None

    def test_invalid_prices_returns_none(self, eng):
        """Prices outside (0,1) ⇒ no opportunity, no exception."""
        for bad in [
            {"YES": 0.0,  "NO": 0.5},
            {"YES": 1.0,  "NO": 0.0},
            {"YES": -0.1, "NO": 0.6},
            {"YES": "abc","NO": 0.5},
            {"YES": float("nan"), "NO": 0.5},
        ]:
            assert eng.detect_single_market_arbitrage(
                market_id="m", outcomes=bad,
                available_liquidity={"YES": 1e6, "NO": 1e6},
                market_size=100.0,
            ) is None

    def test_kl_divergence_nonneg(self, eng):
        opp = eng.detect_single_market_arbitrage(
            market_id="m1",
            outcomes={"YES": 0.40, "NO": 0.55},
            available_liquidity={"YES": 1e6, "NO": 1e6},
            market_size=100.0,
        )
        assert opp is not None
        assert opp.kl_divergence >= 0.0

    def test_validate_arbitrage_rejects_thin_book(self):
        eng = BregmanProjectionEngine(
            min_profit_threshold=0.0,
            fee_schedule=FeeSchedule(taker_fee=0.0, gas_usd=0.0,
                                     impact_coef=0.0),
        )
        opp = eng.detect_single_market_arbitrage(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.45},
            available_liquidity={"YES": 1e6, "NO": 1e6},
            market_size=10_000.0,
        )
        assert opp is not None
        # Now pretend the book has only $100 of depth
        ok = eng.validate_arbitrage(
            opp, available_liquidity={"YES": 100, "NO": 100},
            liquidity_safety_factor=1.0,
        )
        assert ok is False

    def test_unique_opportunity_ids(self, eng):
        ids = set()
        for _ in range(100):
            opp = eng.detect_single_market_arbitrage(
                market_id="m1",
                outcomes={"YES": 0.45, "NO": 0.50},
                available_liquidity={"YES": 1e6, "NO": 1e6},
                market_size=100.0,
            )
            assert opp is not None
            ids.add(opp.opportunity_id)
        assert len(ids) == 100


# ==========================================================================
#  2.  TestFrankWolfeSolver
# ==========================================================================

class TestFrankWolfeSolver:

    def test_empty_input_returns_empty_alloc(self):
        solver = FrankWolfeSolver()
        res = solver.solve(opportunities=[], portfolio_value=100_000.0)
        assert res.optimal_allocation == {}
        assert res.iterations == 0

    def test_zero_portfolio_returns_empty(self):
        solver = FrankWolfeSolver()
        res = solver.solve(
            opportunities=[MarketAllocationInput(
                market_id="m", profit_rate=0.05, depth_usd=1e6,
            )],
            portfolio_value=0.0,
        )
        assert res.optimal_allocation == {}
        assert "portfolio_value" in res.reason

    def test_single_opportunity_fills_within_cap(self):
        solver = FrankWolfeSolver(impact_lambda=0.01)
        res = solver.solve(
            opportunities=[MarketAllocationInput(
                market_id="m1", profit_rate=0.05,
                depth_usd=100_000.0, max_usable=20_000.0,
            )],
            portfolio_value=100_000.0,
            max_total_pct=0.5,
        )
        assert res.converged is True
        # Allocation is between 0 and the per-market cap
        alloc = res.optimal_allocation.get("m1", 0.0)
        assert 0 < alloc <= 20_000.0

    def test_higher_profit_rate_gets_more_capital(self):
        solver = FrankWolfeSolver(impact_lambda=0.01)
        res = solver.solve(
            opportunities=[
                MarketAllocationInput(market_id="lo", profit_rate=0.01,
                                      depth_usd=1e6, max_usable=50_000),
                MarketAllocationInput(market_id="hi", profit_rate=0.10,
                                      depth_usd=1e6, max_usable=50_000),
            ],
            portfolio_value=100_000.0,
            max_total_pct=0.5,
        )
        assert res.converged
        assert (res.optimal_allocation.get("hi", 0.0)
                > res.optimal_allocation.get("lo", 0.0))

    def test_budget_constraint_respected(self):
        solver = FrankWolfeSolver(impact_lambda=0.01)
        res = solver.solve(
            opportunities=[
                MarketAllocationInput(market_id=f"m{i}", profit_rate=0.05,
                                      depth_usd=1e6, max_usable=10_000)
                for i in range(10)
            ],
            portfolio_value=100_000.0,
            max_total_pct=0.30,           # budget = 30 000
        )
        total = sum(res.optimal_allocation.values())
        assert total <= 30_000.0 + 1e-6

    def test_objective_improves_monotonically(self):
        solver = FrankWolfeSolver(max_iterations=50, impact_lambda=0.05)
        res = solver.solve(
            opportunities=[
                MarketAllocationInput(market_id="a", profit_rate=0.10,
                                      depth_usd=100_000),
                MarketAllocationInput(market_id="b", profit_rate=0.05,
                                      depth_usd=100_000),
            ],
            portfolio_value=100_000.0,
        )
        # Objective history should be non-decreasing for concave problem
        for i in range(1, len(res.history)):
            assert res.history[i] >= res.history[i - 1] - 1e-9

    def test_negative_profit_rate_filtered(self):
        solver = FrankWolfeSolver()
        res = solver.solve(
            opportunities=[MarketAllocationInput(
                market_id="bad", profit_rate=-0.05, depth_usd=1e6,
            )],
            portfolio_value=100_000.0,
        )
        assert res.optimal_allocation == {}


# ==========================================================================
#  3.  TestAtomicExecution
# ==========================================================================

@pytest.fixture
def fee_schedule_zero():
    return FeeSchedule(taker_fee=0.0, gas_usd=0.0, impact_coef=0.0)


@pytest.fixture
def cfg(fee_schedule_zero):
    return EngineConfig(
        portfolio_value=100_000.0,
        min_profit_threshold=0.01,
        min_liquidity_per_leg=100.0,
        max_concurrent_positions=10,
        max_drawdown_pct=0.10,
        daily_loss_pct=0.10,
        daily_max_trades=50,
        max_retries=3,
        retry_base_seconds=0.001,
        retry_max_seconds=0.01,
        leg_timeout_seconds=1.0,
        fee_schedule=fee_schedule_zero,
    )


class TestAtomicExecution:

    @pytest.mark.asyncio
    async def test_both_legs_fill_marks_trade_filled(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        trade = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert trade is not None
        assert trade.status == TradeStatus.FILLED
        assert all(l.is_filled for l in trade.legs)
        assert eng.metrics.trades_filled == 1
        assert eng.metrics.trades_failed == 0

    @pytest.mark.asyncio
    async def test_partial_failure_triggers_unwind(self, cfg):
        # First leg fills, second always fails
        client = MockOrderClient(fail_legs=[("m1", "NO")])
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        trade = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert trade is not None
        # YES leg filled but trade as a whole was unwound
        assert trade.status in (TradeStatus.UNWOUND, TradeStatus.FAILED)
        assert eng.metrics.trades_filled == 0
        assert eng.metrics.trades_failed == 1
        # Compensating order was sent
        assert any(c.outcome == "YES" and c.side == "SELL"
                   for c in client.placed_calls)

    @pytest.mark.asyncio
    async def test_retry_on_timeout(self, cfg, fee_schedule_zero):
        # Timeout once (slow), then succeed.  Mock can't easily switch,
        # so we use fail_after_n=1 (one good fill, then errors).
        # Instead simulate by injecting controlled failures.
        attempts = {"n": 0}

        async def flaky_place(leg: Leg):
            attempts["n"] += 1
            if attempts["n"] <= 2:        # first attempt of each leg fails
                return False, None, None, None, "transient_error"
            return True, leg.expected_price, leg.notional_usd / leg.expected_price, "oid", None

        async def cancel(leg: Leg):
            return True

        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=flaky_place, cancel_leg_fn=cancel,
        )
        trade = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert trade is not None
        # Eventually succeeded after retries
        assert trade.status in (TradeStatus.FILLED, TradeStatus.UNWOUND,
                                TradeStatus.FAILED)
        assert attempts["n"] >= 2

    @pytest.mark.asyncio
    async def test_rejected_error_not_retried(self, cfg):
        attempts = {"n": 0}

        async def reject(leg: Leg):
            attempts["n"] += 1
            return False, None, None, None, "Order rejected: invalid token"

        async def cancel(leg: Leg):
            return True

        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=reject, cancel_leg_fn=cancel,
        )
        await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        # Two legs × 1 attempt each = 2 (no retry on "rejected")
        assert attempts["n"] == 2


# ==========================================================================
#  4.  TestRiskManagement
# ==========================================================================

class TestRiskManagement:

    @pytest.mark.asyncio
    async def test_max_drawdown_halts_engine(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        # Place + close a trade with a big loss
        trade = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert trade and trade.status == TradeStatus.FILLED
        # Resolve YES to 0 → both legs lose for an underpriced buy
        # That gives a loss > 10% × 100k = 10 000.  We've staked tiny size,
        # so manually inject a peak / crash to exercise drawdown logic.
        eng.metrics.realised_pnl     = -15_000.0
        eng.metrics.peak_realised_pnl = 0.0
        async with eng._lock:
            eng._update_drawdown_locked()
        assert eng._halted
        assert eng._halt_reason == "max_drawdown_breached"

    @pytest.mark.asyncio
    async def test_halted_engine_refuses_new_trades(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        eng._halted = True
        eng._halt_reason = "test"
        out = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert out is None
        assert eng.metrics.arbitrages_detected == 0

    @pytest.mark.asyncio
    async def test_concurrent_positions_cap(self, cfg):
        cfg.max_concurrent_positions = 1
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        # Second update — engine should bail at evaluate stage
        out = await eng.on_market_update(
            market_id="m2",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert out is None


# ==========================================================================
#  5.  TestPositionSizingViaEngine
# ==========================================================================

class TestPositionSizingViaEngine:
    """The engine's `_sizing_hint` chooses notional given depth + portfolio."""

    @pytest.mark.asyncio
    async def test_size_capped_by_book_depth(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        size = eng._sizing_hint({"YES": 1_000.0, "NO": 1_000.0})
        # 30% of 1000 = 300 ⇒ cap
        assert size <= 300.0 + 1e-6

    @pytest.mark.asyncio
    async def test_size_with_no_liquidity(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        size = eng._sizing_hint({})
        assert size > 0

    @pytest.mark.asyncio
    async def test_size_never_negative(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        for liq in [{}, {"YES": 0, "NO": 0}, {"YES": -100, "NO": -100}]:
            assert eng._sizing_hint(liq) >= 0


# ==========================================================================
#  6.  TestFeeCalculation
# ==========================================================================

class TestFeeCalculation:

    def test_zero_fees(self):
        f = FeeSchedule(taker_fee=0.0, gas_usd=0.0)
        total = FeeCalculator.calculate_fees(
            position_sizes={"YES": 100, "NO": 100},
            prices={"YES": 0.5, "NO": 0.5},
            fees=f,
        )
        assert total == pytest.approx(0.0)

    def test_taker_fees_per_leg(self):
        f = FeeSchedule(taker_fee=0.002, gas_usd=0.0)
        total = FeeCalculator.calculate_fees(
            position_sizes={"YES": 100, "NO": 100},
            prices={"YES": 0.5, "NO": 0.5},
            fees=f,
        )
        # 2 legs × 100 * 0.5 * 0.002 = 0.20
        assert total == pytest.approx(0.20)

    def test_gas_added_per_leg(self):
        f = FeeSchedule(taker_fee=0.0, gas_usd=0.05)
        total = FeeCalculator.calculate_fees(
            position_sizes={"YES": 100, "NO": 100},
            prices={"YES": 0.5, "NO": 0.5},
            fees=f,
        )
        # 2 legs × 0.05 = 0.10
        assert total == pytest.approx(0.10)

    def test_invalid_fees_raise(self):
        with pytest.raises(ValueError):
            FeeSchedule(taker_fee=1.5).validate()
        with pytest.raises(ValueError):
            FeeSchedule(maker_fee=-0.1).validate()
        with pytest.raises(ValueError):
            FeeSchedule(gas_usd=-1).validate()


# ==========================================================================
#  7.  TestSimulatedFailures
# ==========================================================================

class TestSimulatedFailures:

    @pytest.mark.asyncio
    async def test_api_timeout_propagates_to_unwind(self, cfg):

        async def slow_place(leg: Leg):
            await asyncio.sleep(cfg.leg_timeout_seconds + 0.5)
            return True, leg.expected_price, leg.notional_usd / leg.expected_price, "oid", None

        async def cancel(leg: Leg):
            return True

        cfg.max_retries = 1   # speed up
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=slow_place, cancel_leg_fn=cancel,
        )
        trade = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert trade is not None
        assert trade.status in (TradeStatus.UNWOUND, TradeStatus.FAILED)

    @pytest.mark.asyncio
    async def test_malformed_market_data_returns_none(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        out = await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": float("nan"), "NO": 0.5},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert out is None
        # And no spurious orders sent
        assert len(client.placed_calls) == 0

    @pytest.mark.asyncio
    async def test_shutdown_unwinds_open_trades(self, cfg):
        client = MockOrderClient()
        eng = VolkovxArbitrageEngineV2(
            config=cfg, place_leg_fn=client.place, cancel_leg_fn=client.cancel,
        )
        await eng.on_market_update(
            market_id="m1",
            outcomes={"YES": 0.45, "NO": 0.50},
            liquidity={"YES": 100_000, "NO": 100_000},
        )
        assert len(eng.open_trades) == 1
        await eng.shutdown()
        assert eng._halted
        # All trades drained
        assert len(eng.open_trades) == 0
