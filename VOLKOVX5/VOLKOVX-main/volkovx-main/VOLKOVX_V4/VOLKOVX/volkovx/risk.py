"""
volkovx/risk.py — Risk management module (NEW di v4)

Fitur:
  - Dynamic position sizing (Kelly-lite) berdasarkan signal score & balance
  - Consecutive loss tracking → trigger long cooldown
  - Per-window position limit (max 1 bet per 5-min window)
  - Drawdown tracking
  - Heat map (max simultaneous exposure)
"""
import time
import logging
from dataclasses import dataclass, field
from typing import List
from . import config

log = logging.getLogger("volkovx.risk")


@dataclass
class RiskState:
    consec_losses:    int   = 0
    consec_wins:      int   = 0
    long_cooldown_until: float = 0.0
    peak_balance:     float = 0.0
    max_drawdown_pct: float = 0.0
    bets_today:       int   = 0
    last_reset_day:   int   = 0


def calculate_stake(
    base_stake: float,
    balance: float,
    signal_score: float,
    consec_losses: int,
) -> float:
    """Hitung stake untuk bet ini.

    Formula (Kelly-lite):
      stake = base * score_multiplier * loss_dampener
      score_multiplier: 0.5..1.5 berdasarkan signal_score
      loss_dampener:    turun 30% per consec loss (max 50% reduction)

    Selalu clamp ke [MIN_STAKE_USD, MAX_STAKE_PCT * balance]
    """
    if not config.USE_DYNAMIC_SIZING:
        return base_stake

    # Score multiplier: 0.5..1.5
    score_mult = 0.5 + signal_score   # score 0..1 → 0.5..1.5

    # Loss dampener: tiap consec loss kurangi 30%, max 50% total reduction
    loss_mult = max(0.5, 1.0 - 0.30 * consec_losses)

    stake = base_stake * score_mult * loss_mult

    # Clamp ke boundary
    max_allowed = balance * config.MAX_STAKE_PCT if balance > 0 else base_stake
    stake = max(config.MIN_STAKE_USD, min(stake, max_allowed))
    return round(stake, 2)


def update_after_bet(
    risk: RiskState,
    pnl: float,
    current_balance: float,
) -> None:
    """Update risk state setelah satu bet selesai resolve."""
    if pnl > 0:
        risk.consec_wins += 1
        risk.consec_losses = 0
    else:
        risk.consec_losses += 1
        risk.consec_wins = 0

        # Trigger long cooldown
        if risk.consec_losses >= config.MAX_CONSEC_LOSSES:
            risk.long_cooldown_until = time.time() + config.LONG_COOLDOWN_MIN * 60
            log.warning(
                f"🚨 {risk.consec_losses} consec losses — long cooldown "
                f"{config.LONG_COOLDOWN_MIN}min"
            )
            risk.consec_losses = 0  # reset counter setelah cooldown trigger

    # Track drawdown
    if current_balance > risk.peak_balance:
        risk.peak_balance = current_balance
    if risk.peak_balance > 0:
        dd = (risk.peak_balance - current_balance) / risk.peak_balance
        if dd > risk.max_drawdown_pct:
            risk.max_drawdown_pct = dd


def check_circuit_breakers(risk: RiskState, balance: float, start_balance: float) -> tuple:
    """Return (halt_bool, reason) jika ada circuit breaker yang aktif."""
    now = time.time()

    # Long cooldown setelah multiple losses
    if now < risk.long_cooldown_until:
        remaining = int(risk.long_cooldown_until - now)
        return True, f"Long cooldown ({remaining}s remaining)"

    # Daily stop loss
    if start_balance > 0:
        loss_pct = (start_balance - balance) / start_balance
        if loss_pct >= config.DAILY_SL_PCT:
            return True, f"Daily SL hit: -{loss_pct*100:.1f}%"

    # Drawdown circuit breaker (50% dari peak)
    if risk.max_drawdown_pct >= 0.5:
        return True, f"Max drawdown {risk.max_drawdown_pct*100:.1f}% hit"

    return False, ""
