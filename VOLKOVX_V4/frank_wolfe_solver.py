"""
frank_wolfe_solver.py — Multi-Market Capital Allocation Solver
==============================================================

Solves the constrained convex programme that maximises expected post-fee
profit across N simultaneously-detected arbitrage opportunities, subject to
per-market liquidity caps and global portfolio limits.

Problem
-------
    max_x   f(x) = Σ_i  r_i · x_i  −  λ · Σ_i  k_i · x_i² / d_i

    s.t.    0 ≤ x_i ≤ u_i                       (per-market cap)
            Σ_i x_i ≤ B                          (total budget)

    where
        x_i  = USD allocated to market i
        r_i  = post-fee profit *rate*  (profit per $ allocated)
        d_i  = order-book depth (USD) for market i
        k_i  = price-impact coefficient
        u_i  = max usable depth (typically 30% × depth)
        B    = total portfolio cap
        λ    = quadratic-impact penalty weight

The objective is concave (linear − convex quadratic) and the feasible
region is a polytope.  Frank–Wolfe is well-suited:

    1.   Start at x⁰ ∈ feasible.
    2.   Compute gradient ∇f(x).
    3.   Solve LP:  s = argmax_{y ∈ feasible} ∇f(x)·y  (vertex of polytope).
    4.   Line search γ ∈ [0,1]:  γ* = argmax f(x + γ(s − x)).
    5.   Update x ← x + γ*(s − x); check duality gap.
    6.   Stop when gap < tol or max_iter.

For axis-aligned box + budget polytopes the LP has a closed-form solution
("greedy fill highest-gradient first"), so each iteration is O(N).

This implementation is **dependency-free** — it does NOT require numpy
or scipy.  Pure stdlib for portability and minimal attack surface.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
#  Types
# =============================================================================

@dataclass
class MarketAllocationInput:
    """Input descriptor for one candidate allocation slot."""
    market_id:    str
    profit_rate:  float                 # post-fee profit per $ allocated
    depth_usd:    float                 # available order-book depth, USD
    impact_coef:  float = 0.5           # k_i
    max_usable:   Optional[float] = None  # per-market cap; default 30% depth

    def upper_bound(self) -> float:
        if self.max_usable is not None and self.max_usable >= 0:
            return float(self.max_usable)
        return max(0.0, self.depth_usd) * 0.30

    def validate(self) -> None:
        if self.profit_rate < 0:
            raise ValueError(f"profit_rate negative: {self.profit_rate}")
        if self.depth_usd < 0:
            raise ValueError(f"depth_usd negative: {self.depth_usd}")
        if self.impact_coef < 0:
            raise ValueError(f"impact_coef negative: {self.impact_coef}")


@dataclass
class OptimizationResult:
    """Outcome of a Frank–Wolfe solve."""
    converged:           bool
    iterations:          int
    optimal_allocation:  Dict[str, float]   # {market_id: usd}
    objective_value:     float
    duality_gap:         float
    elapsed_seconds:     float
    reason:              str = ""           # explanation if not converged
    history:             List[float] = field(default_factory=list)


# =============================================================================
#  Solver
# =============================================================================

class FrankWolfeSolver:
    """
    Frank–Wolfe (a.k.a. conditional-gradient) solver tailored to the
    arbitrage allocation problem above.

    Parameters
    ----------
    max_iterations    : hard cap on iterations (typical: 50–150)
    convergence_gap   : stop when duality gap / objective < this (e.g. 1e-3)
    timeout_seconds   : wall-clock cap; respected at iteration boundaries
    impact_lambda     : λ in the objective; higher → more conservative

    Thread-safety: the solver is stateless apart from its three constructor
    parameters.  A single instance can be shared across async tasks.
    """

    def __init__(
        self,
        max_iterations:  int   = 100,
        convergence_gap: float = 1e-3,
        timeout_seconds: float = 30.0,
        impact_lambda:   float = 1.0,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be > 0")
        if convergence_gap <= 0:
            raise ValueError("convergence_gap must be > 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if impact_lambda < 0:
            raise ValueError("impact_lambda must be >= 0")

        self.max_iterations  = int(max_iterations)
        self.convergence_gap = float(convergence_gap)
        self.timeout_seconds = float(timeout_seconds)
        self.impact_lambda   = float(impact_lambda)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def solve(
        self,
        opportunities:   List[MarketAllocationInput],
        portfolio_value: float,
        max_total_pct:   float = 0.50,
    ) -> OptimizationResult:
        """
        Allocate capital across opportunities.

        Returns an OptimizationResult.  Never raises — failure modes
        produce a non-converged result with a `reason` field.
        """
        t0 = time.time()
        try:
            return self._solve(
                opportunities, portfolio_value, max_total_pct, t0,
            )
        except Exception as exc:
            logger.error("FrankWolfe solve crashed: %s", exc, exc_info=True)
            return OptimizationResult(
                converged=False, iterations=0,
                optimal_allocation={}, objective_value=0.0,
                duality_gap=math.inf,
                elapsed_seconds=time.time() - t0,
                reason=f"crashed: {exc}",
            )

    # ------------------------------------------------------------------
    #  Core algorithm
    # ------------------------------------------------------------------

    def _solve(
        self,
        opportunities:   List[MarketAllocationInput],
        portfolio_value: float,
        max_total_pct:   float,
        t0:              float,
    ) -> OptimizationResult:
        if portfolio_value <= 0:
            return self._empty_result(t0, "portfolio_value <= 0")
        if not 0 < max_total_pct <= 1.0:
            return self._empty_result(t0, "max_total_pct must be in (0,1]")

        # Filter & validate input
        cands: List[MarketAllocationInput] = []
        for opp in opportunities:
            try:
                opp.validate()
            except Exception as e:
                logger.warning("skipping invalid candidate %s: %s",
                               getattr(opp, "market_id", "?"), e)
                continue
            if opp.profit_rate <= 0 or opp.upper_bound() <= 0:
                continue
            cands.append(opp)

        if not cands:
            return self._empty_result(t0, "no valid candidates")

        budget = portfolio_value * max_total_pct
        n      = len(cands)

        r  = [c.profit_rate for c in cands]
        d  = [max(c.depth_usd, 1e-9) for c in cands]
        k  = [c.impact_coef for c in cands]
        ub = [c.upper_bound() for c in cands]

        # Initial feasible point: zero allocation everywhere
        x = [0.0] * n

        history: List[float] = []
        gap = math.inf

        for it in range(1, self.max_iterations + 1):
            # Timeout check
            if time.time() - t0 > self.timeout_seconds:
                return OptimizationResult(
                    converged=False, iterations=it,
                    optimal_allocation=self._to_dict(cands, x),
                    objective_value=self._objective(x, r, k, d),
                    duality_gap=gap,
                    elapsed_seconds=time.time() - t0,
                    reason="timeout",
                    history=history,
                )

            # ---- gradient ---------------------------------------------
            #   ∂f/∂x_i = r_i − 2λ·k_i·x_i / d_i
            grad = [
                r[i] - 2.0 * self.impact_lambda * k[i] * x[i] / d[i]
                for i in range(n)
            ]

            # ---- Frank–Wolfe vertex: greedy fill on positive grad ----
            s = self._greedy_lp_solution(grad, ub, budget)

            # ---- duality gap = ⟨∇f(x), s − x⟩ ------------------------
            gap = sum(grad[i] * (s[i] - x[i]) for i in range(n))

            f_val = self._objective(x, r, k, d)
            history.append(f_val)

            if gap <= self.convergence_gap * max(1.0, abs(f_val)):
                return OptimizationResult(
                    converged=True, iterations=it,
                    optimal_allocation=self._to_dict(cands, x),
                    objective_value=f_val,
                    duality_gap=gap,
                    elapsed_seconds=time.time() - t0,
                    reason="converged",
                    history=history,
                )

            # ---- line search γ ∈ [0,1] -------------------------------
            #   f(x + γ(s−x)) is concave quadratic in γ ⇒ closed form
            gamma = self._line_search(x, s, r, k, d)
            if gamma <= 0:
                return OptimizationResult(
                    converged=True, iterations=it,
                    optimal_allocation=self._to_dict(cands, x),
                    objective_value=f_val,
                    duality_gap=gap,
                    elapsed_seconds=time.time() - t0,
                    reason="line_search_zero",
                    history=history,
                )

            # ---- update --------------------------------------------
            x = [x[i] + gamma * (s[i] - x[i]) for i in range(n)]

        # Hit max_iterations
        return OptimizationResult(
            converged=False,
            iterations=self.max_iterations,
            optimal_allocation=self._to_dict(cands, x),
            objective_value=self._objective(x, r, k, d),
            duality_gap=gap,
            elapsed_seconds=time.time() - t0,
            reason="max_iterations",
            history=history,
        )

    # ------------------------------------------------------------------
    #  Sub-routines
    # ------------------------------------------------------------------

    @staticmethod
    def _greedy_lp_solution(
        grad:   List[float],
        ub:     List[float],
        budget: float,
    ) -> List[float]:
        """
        For the polytope {0 ≤ x ≤ ub, Σx ≤ budget}, the LP

            argmax_y  ⟨grad, y⟩

        has a closed-form solution: rank by `grad` descending and fill
        each component up to ub_i until the budget is exhausted.
        Negative-gradient components stay at zero.
        """
        n = len(grad)
        order = sorted(range(n), key=lambda i: grad[i], reverse=True)
        s = [0.0] * n
        remaining = max(0.0, budget)
        for i in order:
            if grad[i] <= 0 or remaining <= 0:
                break
            take = min(ub[i], remaining)
            if take <= 0:
                continue
            s[i] = take
            remaining -= take
        return s

    def _line_search(
        self,
        x: List[float], s: List[float],
        r: List[float], k: List[float], d: List[float],
    ) -> float:
        """
        Closed-form maximiser of f(x + γ(s−x)) on [0,1] for our objective.

        f(γ) = Σ_i r_i (x_i + γ Δ_i)
             − λ Σ_i (k_i / d_i) (x_i + γ Δ_i)²
             where Δ_i = s_i − x_i

        df/dγ = Σ r_i Δ_i − 2λ Σ (k_i/d_i)(x_i + γΔ_i)Δ_i
              = A − 2λ (B + γ C)

        where
            A = Σ r_i Δ_i
            B = Σ (k_i/d_i) x_i Δ_i
            C = Σ (k_i/d_i) Δ_i²

        γ* = (A − 2λB) / (2λ C)   if  C > 0,  clipped to [0,1]
        """
        n = len(x)
        delta = [s[i] - x[i] for i in range(n)]
        A = sum(r[i] * delta[i] for i in range(n))
        if A <= 0:
            return 0.0

        B = sum((k[i] / d[i]) * x[i] * delta[i] for i in range(n))
        C = sum((k[i] / d[i]) * delta[i] * delta[i] for i in range(n))

        denom = 2.0 * self.impact_lambda * C
        if denom <= 1e-18:
            # Linear in γ ⇒ go all the way
            return 1.0

        gamma = (A - 2.0 * self.impact_lambda * B) / denom
        return max(0.0, min(1.0, gamma))

    def _objective(
        self,
        x: List[float],
        r: List[float], k: List[float], d: List[float],
    ) -> float:
        linear  = sum(r[i] * x[i] for i in range(len(x)))
        quad    = sum((k[i] / d[i]) * x[i] * x[i] for i in range(len(x)))
        return linear - self.impact_lambda * quad

    @staticmethod
    def _to_dict(
        cands: List[MarketAllocationInput],
        x:     List[float],
    ) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for c, v in zip(cands, x):
            if v > 0:
                out[c.market_id] = float(v)
        return out

    @staticmethod
    def _empty_result(t0: float, reason: str) -> OptimizationResult:
        return OptimizationResult(
            converged=False,
            iterations=0,
            optimal_allocation={},
            objective_value=0.0,
            duality_gap=math.inf,
            elapsed_seconds=time.time() - t0,
            reason=reason,
        )
