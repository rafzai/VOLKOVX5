"""
Position Sizing Engine - Risk-Adjusted Kelly Criterion
Calculates trade sizes accounting for execution risk and order book constraints

Kelly Criterion Formula:
  f* = (bp - q) / b
  
  Where:
    f* = fraction of bankroll to wager
    b = odds (ratio of win/loss)
    p = probability of win
    q = probability of loss (1-p)

Arbitrage Adjustment:
  In pure arbitrage (guaranteed profit):
    p = 1.0 (always win)
    q = 0.0
    
  But with execution risk:
    p = (1 - execution_failure_rate)
    q = execution_failure_rate
  
  Conservative: Use fractional Kelly (f = 0.25 * f*) to reduce variance

Research: Top traders used conservative Kelly with 0.25-0.5 fraction
"""

import logging
import math
from typing import Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class PositionSize:
    """Calculated position size"""
    outcome_name: str
    base_size: float
    kelly_size: float
    final_size: float
    order_book_constraint: float
    execution_risk_adjustment: float

class PositionSizingEngine:
    """
    Calculate optimal position sizes with risk management
    """
    
    def __init__(
        self,
        portfolio_value: float = 100_000,
        kelly_fraction: float = 0.25,
        max_portfolio_allocation: float = 0.05,
        order_book_depth_max: float = 0.30
    ):
        self.portfolio_value = portfolio_value
        self.kelly_fraction = kelly_fraction
        self.max_portfolio_allocation = max_portfolio_allocation
        self.order_book_depth_max = order_book_depth_max
    
    def calculate_position_size(
        self,
        guaranteed_profit: float,
        current_prices: Dict[str, float],
        optimal_prices: Dict[str, float],
        order_book_liquidity: Dict[str, float],
        execution_risk: float = 0.02,
        slippage: float = 0.001
    ) -> PositionSize:
        """
        Calculate optimal position size
        
        Args:
            guaranteed_profit: Profit from arbitrage (before size)
            current_prices: Current market prices
            optimal_prices: Optimal arbitrage-free prices
            order_book_liquidity: Available liquidity per outcome
            execution_risk: Probability execution fails (default 2%)
            slippage: Expected slippage on execution
        
        Returns:
            PositionSize with sizing recommendations
        """
        
        try:
            # Step 1: Calculate Kelly-optimal size
            kelly_size = self._calculate_kelly_size(
                guaranteed_profit,
                current_prices,
                optimal_prices,
                execution_risk
            )
            
            # Step 2: Apply Kelly fraction (conservative)
            fractional_kelly_size = kelly_size * self.kelly_fraction
            
            # Step 3: Adjust for order book depth
            order_book_adjusted = self._apply_order_book_constraints(
                fractional_kelly_size,
                order_book_liquidity
            )
            
            # Step 4: Apply portfolio allocation limit
            max_allocation_dollars = self.portfolio_value * self.max_portfolio_allocation
            final_size = min(order_book_adjusted, max_allocation_dollars)
            
            # Step 5: Account for slippage
            final_size = final_size * (1 - slippage)
            
            # Determine position name
            position_name = 'YES' if guaranteed_profit > 0 else 'NO'
            
            logger.info(
                f"📊 Position Sizing Calculated:\n"
                f"   Kelly optimal: ${kelly_size:,.2f}\n"
                f"   Fractional Kelly (1/4): ${fractional_kelly_size:,.2f}\n"
                f"   Order book limited: ${order_book_adjusted:,.2f}\n"
                f"   Final size: ${final_size:,.2f}\n"
                f"   Position: {position_name}"
            )
            
            return PositionSize(
                outcome_name=position_name,
                base_size=guaranteed_profit,
                kelly_size=kelly_size,
                final_size=final_size,
                order_book_constraint=order_book_adjusted / kelly_size if kelly_size > 0 else 1.0,
                execution_risk_adjustment=execution_risk
            )
        
        except Exception as e:
            logger.error(f"Position sizing error: {e}")
            # Return minimal position on error
            return PositionSize(
                outcome_name='YES',
                base_size=0,
                kelly_size=0,
                final_size=0,
                order_book_constraint=0,
                execution_risk_adjustment=execution_risk
            )
    
    def _calculate_kelly_size(
        self,
        guaranteed_profit: float,
        current_prices: Dict[str, float],
        optimal_prices: Dict[str, float],
        execution_risk: float
    ) -> float:
        """
        Calculate Kelly-optimal size
        
        Kelly Formula for arbitrage:
          f* = (guaranteed_profit) / portfolio_value
          
        Adjusted for execution risk:
          p = 1 - execution_risk
          f* = (p * profit - (1-p) * loss) / portfolio_value
        """
        
        if self.portfolio_value <= 0:
            return 0
        
        # Execution adjustment
        p = 1.0 - execution_risk  # Probability of success
        
        # Expected profit on unit bet
        expected_profit_per_unit = p * guaranteed_profit
        
        # Expected loss on failure
        expected_loss_per_unit = (1 - p) * guaranteed_profit
        
        # Kelly fraction (before conservative scaling)
        if expected_loss_per_unit == 0:
            # Riskless: can bet full portfolio
            kelly_fraction = 1.0
        else:
            kelly_fraction = expected_profit_per_unit / expected_loss_per_unit
        
        # Size = Kelly fraction * portfolio
        kelly_size = kelly_fraction * self.portfolio_value
        
        return max(0, kelly_size)
    
    def _apply_order_book_constraints(
        self,
        suggested_size: float,
        liquidity: Dict[str, float]
    ) -> float:
        """
        Constrain position size to not exceed order book depth
        
        Constraint: Don't use >30% of available depth
        (to avoid moving market against ourselves)
        """
        
        max_by_liquidity = float('inf')
        
        for outcome, available in liquidity.items():
            # Can use at most 30% of available depth
            max_this_outcome = available * self.order_book_depth_max
            max_by_liquidity = min(max_by_liquidity, max_this_outcome)
        
        if max_by_liquidity == float('inf'):
            return suggested_size
        
        return min(suggested_size, max_by_liquidity)
    
    def scale_position_for_portfolio(
        self,
        base_position: float,
        current_portfolio_value: Optional[float] = None
    ) -> float:
        """
        Rescale position based on current portfolio (for PnL updates)
        """
        
        portfolio = current_portfolio_value or self.portfolio_value
        
        if self.portfolio_value <= 0:
            return base_position
        
        scale_factor = portfolio / self.portfolio_value
        return base_position * scale_factor

class ExecutionRiskAssessment:
    """
    Estimate execution risk based on market conditions
    
    Factors:
    - Spread: wider = higher risk
    - Liquidity: lower = higher risk
    - Latency: higher = higher risk
    - Market volatility
    """
    
    @staticmethod
    def estimate_failure_probability(
        bid_ask_spread: float,
        available_liquidity: float,
        latency_ms: int,
        volatility: float = 0.01
    ) -> float:
        """
        Estimate probability execution will fail
        
        Returns: float in [0, 1]
        """
        
        failure_prob = 0.0
        
        # Spread contribution: wider spreads = higher failure
        # Spread of 1% = 0.5% failure
        # Spread of 10% = 5% failure
        spread_contribution = bid_ask_spread / 2
        failure_prob += spread_contribution
        
        # Liquidity contribution: lower liquidity = higher failure
        # <$1000 = 3% failure
        # <$500 = 5% failure
        if available_liquidity < 500:
            failure_prob += 0.05
        elif available_liquidity < 1000:
            failure_prob += 0.03
        
        # Latency contribution
        # 50ms = 0% failure
        # 500ms = 2% failure
        # 1000ms = 4% failure
        latency_failure = max(0, (latency_ms - 50) / 500 * 0.02)
        failure_prob += latency_failure
        
        # Cap at 10%
        return min(0.10, failure_prob)

class KellyCalculator:
    """
    Pure Kelly criterion calculator
    
    Provides utilities for:
    - Kelly fraction calculation
    - Fractional Kelly conversion
    - Growth rate analysis
    """
    
    @staticmethod
    def calculate_growth_rate(
        kelly_fraction: float,
        win_probability: float,
        profit_ratio: float
    ) -> float:
        """
        Calculate expected logarithmic growth rate
        
        Growth = p * log(1 + b*f) + (1-p) * log(1 - f)
        
        Where:
            f = Kelly fraction
            p = win probability
            b = profit ratio (profit / loss)
        """
        
        if win_probability < 0 or win_probability > 1:
            return 0
        
        try:
            growth = (
                win_probability * math.log(1 + profit_ratio * kelly_fraction) +
                (1 - win_probability) * math.log(1 - kelly_fraction)
            )
            return growth
        except ValueError:
            return 0
    
    @staticmethod
    def calculate_ruin_probability(
        kelly_fraction: float,
        win_probability: float,
        n_bets: int
    ) -> float:
        """
        Calculate probability of ruin over n bets
        
        Approximation: P(ruin) ≈ (1-f)^n for small f
        """
        
        if kelly_fraction <= 0 or kelly_fraction >= 1:
            return 0
        
        return (1 - kelly_fraction) ** n_bets
