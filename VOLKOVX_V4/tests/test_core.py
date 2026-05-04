"""
tests/test_core.py — Unit test untuk logic kritis VOLKOVX v4

Jalankan: python -m pytest tests/ -v
Atau langsung: python tests/test_core.py
"""
import sys
import time
import unittest
from pathlib import Path

# Tambah parent dir ke path supaya bisa import volkovx
sys.path.insert(0, str(Path(__file__).parent.parent))

from volkovx import config
from volkovx.feeds import (
    STATE, _safe_float, _aggregate_liq, _aggregate_cvd,
    _LIQ_BUFFER, _TRADE_BUFFER, _PRICE_HISTORY, _compute_volatility,
    _is_btc_5min_market,
)
from volkovx.filters import run_all_filters, _normalize
from volkovx.risk import (
    RiskState, calculate_stake, update_after_bet, check_circuit_breakers
)
from volkovx.session import SessionStats, BetRecord


# ─────────────────────────────────────────────────────────────
#  Test: _safe_float
# ─────────────────────────────────────────────────────────────
class TestSafeFloat(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(_safe_float("3.14"), 3.14)
        self.assertEqual(_safe_float(42), 42.0)

    def test_invalid(self):
        self.assertEqual(_safe_float(None), 0.0)
        self.assertEqual(_safe_float(""), 0.0)
        self.assertEqual(_safe_float("not a number"), 0.0)
        self.assertEqual(_safe_float("nan"), 0.0)
        self.assertEqual(_safe_float("inf"), 0.0)
        self.assertEqual(_safe_float("-inf"), 0.0)

    def test_default(self):
        self.assertEqual(_safe_float(None, default=99.9), 99.9)


# ─────────────────────────────────────────────────────────────
#  Test: liquidation aggregator
# ─────────────────────────────────────────────────────────────
class TestLiqAggregator(unittest.TestCase):
    def setUp(self):
        _LIQ_BUFFER.clear()

    def test_empty(self):
        s3, s30, l3, l30 = _aggregate_liq(time.time())
        self.assertEqual((s3, s30, l3, l30), (0, 0, 0, 0))

    def test_within_3s(self):
        now = time.time()
        _LIQ_BUFFER.append((now - 1,  "A", 5000))   # short liq
        _LIQ_BUFFER.append((now - 2,  "B", 3000))   # long liq
        s3, s30, l3, l30 = _aggregate_liq(now)
        self.assertEqual(s3, 5000)
        self.assertEqual(l3, 3000)
        self.assertEqual(s30, 5000)
        self.assertEqual(l30, 3000)

    def test_30s_window(self):
        now = time.time()
        _LIQ_BUFFER.append((now - 25, "A", 10000))  # masuk 30s, bukan 3s
        _LIQ_BUFFER.append((now - 1,  "A", 2000))
        s3, s30, l3, l30 = _aggregate_liq(now)
        self.assertEqual(s3, 2000)
        self.assertEqual(s30, 12000)

    def test_old_pruned(self):
        now = time.time()
        _LIQ_BUFFER.append((now - 100, "A", 99999))  # > 30s, harus di-prune
        _LIQ_BUFFER.append((now - 5,   "A", 1000))
        s3, s30, l3, l30 = _aggregate_liq(now)
        self.assertEqual(s30, 1000)
        # Buffer harus sudah di-prune
        self.assertEqual(len(_LIQ_BUFFER), 1)


# ─────────────────────────────────────────────────────────────
#  Test: CVD aggregator
# ─────────────────────────────────────────────────────────────
class TestCVDAggregator(unittest.TestCase):
    def setUp(self):
        _TRADE_BUFFER.clear()

    def test_buys_minus_sells(self):
        now = time.time()
        _TRADE_BUFFER.append((now - 5,  "B", 10000))  # buy
        _TRADE_BUFFER.append((now - 5,  "A", 3000))   # sell
        cvd_30s, cvd_2m = _aggregate_cvd(now)
        self.assertEqual(cvd_30s, 7000)
        self.assertEqual(cvd_2m,  7000)

    def test_2m_vs_30s_split(self):
        now = time.time()
        _TRADE_BUFFER.append((now - 60, "B", 5000))   # masuk 2m, bukan 30s
        _TRADE_BUFFER.append((now - 10, "B", 1000))
        cvd_30s, cvd_2m = _aggregate_cvd(now)
        self.assertEqual(cvd_30s, 1000)
        self.assertEqual(cvd_2m,  6000)


# ─────────────────────────────────────────────────────────────
#  Test: market discovery
# ─────────────────────────────────────────────────────────────
class TestMarketDiscovery(unittest.TestCase):
    def test_match_btc_5min(self):
        m = {"question": "Bitcoin Up or Down 5 minute window starting...",
             "slug": "btc-up-or-down-5min"}
        self.assertTrue(_is_btc_5min_market(m))

    def test_skip_other_market(self):
        m = {"question": "Will ETH go up?", "slug": "eth-prediction"}
        self.assertFalse(_is_btc_5min_market(m))


# ─────────────────────────────────────────────────────────────
#  Test: filter normalize
# ─────────────────────────────────────────────────────────────
class TestNormalize(unittest.TestCase):
    def test_at_target(self):
        self.assertEqual(_normalize(50, 50), 0.5)

    def test_zero(self):
        self.assertEqual(_normalize(0, 50), 0.0)

    def test_above_cap(self):
        self.assertEqual(_normalize(500, 50, cap=3.0), 1.0)

    def test_invalid_target(self):
        self.assertEqual(_normalize(10, 0), 0.0)


# ─────────────────────────────────────────────────────────────
#  Test: risk management
# ─────────────────────────────────────────────────────────────
class TestRiskMgmt(unittest.TestCase):
    def test_static_sizing(self):
        config.USE_DYNAMIC_SIZING = False
        stake = calculate_stake(2.0, 100, 0.7, 0)
        self.assertEqual(stake, 2.0)

    def test_dynamic_sizing_high_score(self):
        config.USE_DYNAMIC_SIZING = True
        config.MIN_STAKE_USD = 1.0
        config.MAX_STAKE_PCT = 0.10
        stake = calculate_stake(2.0, 1000, 1.0, 0)
        # base * (0.5 + score) * loss_mult = 2 * 1.5 * 1.0 = 3.0
        # max allowed = 1000 * 0.10 = 100, so 3.0 fits
        self.assertEqual(stake, 3.0)

    def test_dynamic_sizing_after_losses(self):
        config.USE_DYNAMIC_SIZING = True
        config.MIN_STAKE_USD = 0.5
        config.MAX_STAKE_PCT = 0.10
        # base * (0.5 + score) * max(0.5, 1 - 0.30*losses)
        # = 2 * 1.0 * max(0.5, 0.4) = 2 * 1.0 * 0.5 = 1.0
        stake = calculate_stake(2.0, 1000, 0.5, 2)
        self.assertAlmostEqual(stake, 1.0, places=2)

    def test_consec_loss_trigger(self):
        config.MAX_CONSEC_LOSSES = 3
        config.LONG_COOLDOWN_MIN = 1
        risk = RiskState()
        for _ in range(3):
            update_after_bet(risk, -1.0, 100)
        self.assertGreater(risk.long_cooldown_until, time.time())

    def test_circuit_breaker_daily_sl(self):
        config.DAILY_SL_PCT = 0.50
        risk = RiskState()
        # balance turun 60% dari start
        halt, reason = check_circuit_breakers(risk, 40, 100)
        self.assertTrue(halt)
        self.assertIn("Daily SL", reason)


# ─────────────────────────────────────────────────────────────
#  Test: session bet recording (no duplicates!)
# ─────────────────────────────────────────────────────────────
class TestSessionStats(unittest.TestCase):
    def test_bet_recorded_once(self):
        """Regression test for v3 bug: bet was appended 2x."""
        stats = SessionStats()
        bet = BetRecord(
            ts=time.time(), window_ts=time.time(), direction="UP",
            beat_price=50000, stake=2.0, odds=0.5, order_id="TEST-1"
        )
        stats.record_bet(bet)
        self.assertEqual(len(stats.bets), 1)

    def test_resolve_idempotent(self):
        """Resolving same bet twice should be no-op."""
        stats = SessionStats()
        bet = BetRecord(
            ts=time.time(), window_ts=time.time(), direction="UP",
            beat_price=50000, stake=2.0, odds=0.5, order_id="T1"
        )
        stats.record_bet(bet)
        stats.resolve_bet(bet, +1.0)
        self.assertEqual(stats.wins, 1)
        # Resolve lagi → tidak boleh nambah win
        stats.resolve_bet(bet, +1.0)
        self.assertEqual(stats.wins, 1)

    def test_pnl_accumulate(self):
        stats = SessionStats()
        for i, pnl in enumerate([+1.0, -2.0, +1.5]):
            bet = BetRecord(
                ts=time.time(), window_ts=time.time(), direction="UP",
                beat_price=50000, stake=2.0, odds=0.5, order_id=f"T{i}"
            )
            stats.record_bet(bet)
            stats.resolve_bet(bet, pnl)
        self.assertAlmostEqual(stats.pnl, 0.5, places=2)
        self.assertEqual(stats.wins, 2)
        self.assertEqual(stats.losses, 1)

    def test_daily_sl(self):
        config.DAILY_SL_PCT = 0.50
        stats = SessionStats()
        stats.start_balance   = 100.0
        stats.current_balance = 49.0  # turun > 50%
        self.assertTrue(stats.check_daily_stop_loss())
        self.assertTrue(stats.halted)


# ─────────────────────────────────────────────────────────────
#  Test: filter pipeline
# ─────────────────────────────────────────────────────────────
class TestFilterPipeline(unittest.TestCase):
    def setUp(self):
        # Reset state
        STATE.btc_price_hl = 50_100.0
        STATE.price_ts_hl  = time.time()
        STATE.liq_short_3s  = 20_000
        STATE.liq_short_30s = 80_000
        STATE.liq_long_3s   = 0
        STATE.liq_long_30s  = 0
        STATE.cvd_2min      = 30_000
        STATE.cvd_30s       = 10_000
        STATE.odds_up       = 0.45
        STATE.odds_down     = 0.55
        STATE.obi           = 0.3
        STATE.token_up      = "tok_up"
        STATE.token_down    = "tok_down"

    def test_all_pass(self):
        result = run_all_filters(
            window_elapsed=240,
            opening_price=50_000.0,
            last_bet_ts=0,
            gocek_freeze_until=0,
        )
        self.assertTrue(result.passed, f"Expected pass, got: {result.skip_reason}")
        self.assertEqual(result.direction, "UP")
        self.assertGreater(result.score, config.F8_MIN_SIGNAL_SCORE)

    def test_skip_outside_zone(self):
        result = run_all_filters(
            window_elapsed=100,  # too early
            opening_price=50_000.0,
            last_bet_ts=0,
            gocek_freeze_until=0,
        )
        self.assertFalse(result.passed)
        self.assertIn("zone", result.skip_reason.lower())

    def test_skip_small_distance(self):
        STATE.btc_price_hl = 50_010.0  # only +$10, < $40 min
        result = run_all_filters(
            window_elapsed=240, opening_price=50_000.0,
            last_bet_ts=0, gocek_freeze_until=0,
        )
        self.assertFalse(result.passed)

    def test_skip_misaligned_cvd(self):
        STATE.cvd_2min = -50_000  # CVD bilang DOWN tapi liq bilang UP
        result = run_all_filters(
            window_elapsed=240, opening_price=50_000.0,
            last_bet_ts=0, gocek_freeze_until=0,
        )
        self.assertFalse(result.passed)
        self.assertIn("CVD", result.skip_reason)

    def test_skip_extreme_odds(self):
        STATE.odds_up = 0.95  # too expensive
        result = run_all_filters(
            window_elapsed=240, opening_price=50_000.0,
            last_bet_ts=0, gocek_freeze_until=0,
        )
        self.assertFalse(result.passed)

    def test_skip_stale_price(self):
        STATE.price_ts_hl = time.time() - 100  # 100s old
        STATE.price_ts_poly = 0
        STATE.price_ts_chain = 0
        STATE.price_ts_bin = 0
        result = run_all_filters(
            window_elapsed=240, opening_price=50_000.0,
            last_bet_ts=0, gocek_freeze_until=0,
        )
        self.assertFalse(result.passed)


# ─────────────────────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)
