"""
volkovx/filters.py — Multi-filter dengan Composite Signal Score (v4)

Filter pipeline:
  F1  Entry zone timing
  F2  Beat distance (price moved enough from window open)
  F3  Liquidation cascade dual-window (3s + 30s)
  F4  CVD alignment (delta order flow agrees with direction)
  F5  Odds sanity (avoid extreme prices = bad RR)
  F6  Gocek cooldown
  F7  Data freshness (no stale price)
  F8  Composite signal score (weighted sum dari semua sinyal)

Direction agreement: F2 ≥ F3 ≥ F4 ≥ OBI semua harus searah,
kalau ada conflict → reject. Score = bobot kekuatan sinyal yang searah.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict
from . import config
from .feeds import STATE as feed

log = logging.getLogger("volkovx.filters")


@dataclass
class FilterResult:
    passed:    bool
    direction: Optional[str]   # "UP" | "DOWN" | None
    score:     float = 0.0     # 0..1 composite confidence
    f1: str = ""
    f2: str = ""
    f3: str = ""
    f4: str = ""
    f5: str = ""
    f6: str = ""
    f7: str = ""
    f8: str = ""
    skip_reason: str = ""
    components:  Dict[str, float] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def _normalize(v: float, target: float, cap: float = 3.0) -> float:
    """Map nilai ke [0, 1] berdasarkan rasio terhadap target.
    v=target → 0.5, v=cap*target → 1.0, v=0 → 0.0
    """
    if target <= 0:
        return 0.0
    ratio = v / target
    if ratio <= 0:
        return 0.0
    if ratio >= cap:
        return 1.0
    # smooth: 0..1 → 0..0.5, 1..cap → 0.5..1.0
    if ratio <= 1:
        return 0.5 * ratio
    return 0.5 + 0.5 * (ratio - 1) / (cap - 1)


def _direction_strength(value: float, threshold_ratio: float = 1.3) -> Optional[str]:
    """Return 'UP'/'DOWN'/None berdasarkan tanda dan magnitudo."""
    if value > 0:
        return "UP"
    if value < 0:
        return "DOWN"
    return None


# ─────────────────────────────────────────────────────────────
#  Main filter pipeline
# ─────────────────────────────────────────────────────────────
def run_all_filters(
    window_elapsed:     float,
    opening_price:      Optional[float],
    last_bet_ts:        float,
    gocek_freeze_until: float,
) -> FilterResult:
    now = time.time()
    snap = feed.snapshot()
    result = FilterResult(passed=False, direction=None)

    # ── F1: Entry Zone ──────────────────────────────────────
    in_zone = config.ENTRY_START_SEC <= window_elapsed <= config.ENTRY_END_SEC
    result.f1 = (
        f"t={window_elapsed:.0f}s ✅ IN ZONE"
        if in_zone else
        f"t={window_elapsed:.0f}s ❌ wait [{config.ENTRY_START_SEC}-{config.ENTRY_END_SEC}s]"
    )
    if not in_zone:
        result.skip_reason = "Outside entry zone"
        return result

    # ── F7: Data freshness ──────────────────────────────────
    price_age = snap["price_age"]
    fresh_ok  = price_age <= config.F7_PRICE_STALENESS_S
    result.f7 = (
        f"price age {price_age:.1f}s ✅"
        if fresh_ok else
        f"price age {price_age:.1f}s ❌ stale (max {config.F7_PRICE_STALENESS_S}s)"
    )
    if not fresh_ok:
        result.skip_reason = f"Stale price ({price_age:.1f}s)"
        return result

    # ── F2: Beat Distance ───────────────────────────────────
    current_price = snap["btc_price"]
    if not current_price or not opening_price:
        result.f2 = "❌ No price data"
        result.skip_reason = "No price data"
        return result

    beat_dist = current_price - opening_price
    abs_dist  = abs(beat_dist)
    dist_ok   = abs_dist >= config.F2_BEAT_DIST_MIN
    direction_f2 = "UP" if beat_dist > 0 else "DOWN"
    result.f2 = (
        f"{'+' if beat_dist >= 0 else '-'}${abs_dist:.1f} "
        f"{'✅' if dist_ok else '❌'} "
        f"(need ≥${config.F2_BEAT_DIST_MIN:.0f}) → {direction_f2}"
    )
    if not dist_ok:
        result.skip_reason = f"Beat dist ${abs_dist:.1f} < ${config.F2_BEAT_DIST_MIN}"
        return result

    # ── F3: Liquidation Dual-window ─────────────────────────
    liq_s3, liq_s30 = snap["liq_short_3s"], snap["liq_short_30s"]
    liq_l3, liq_l30 = snap["liq_long_3s"],  snap["liq_long_30s"]

    recent    = max(liq_s3, liq_l3)
    sustained = max(liq_s30, liq_l30)
    recent_ok    = recent    >= config.F3_LIQ_3S_MIN
    sustained_ok = sustained >= config.F3_LIQ_30S_MIN
    f3_ok = recent_ok and sustained_ok

    result.f3 = (
        f"3s: ${recent:,.0f}{'✅' if recent_ok else '❌'} | "
        f"30s: ${sustained:,.0f}{'✅' if sustained_ok else '❌'}"
    )
    if not f3_ok:
        result.skip_reason = "Liquidation under threshold"
        return result

    # Direction dari liquidation (short liq → UP, long liq → DOWN)
    if liq_s30 > liq_l30 * 1.3:
        direction_f3 = "UP"
    elif liq_l30 > liq_s30 * 1.3:
        direction_f3 = "DOWN"
    else:
        result.skip_reason = "Liquidation direction unclear"
        result.f3 += " | dir unclear"
        return result

    # ── F4: CVD Alignment ───────────────────────────────────
    cvd      = snap["cvd_2min"]
    cvd_30s  = snap["cvd_30s"]
    cvd_dir  = "UP" if cvd > 0 else "DOWN"
    cvd_ok   = abs(cvd) >= config.F4_CVD_MIN and cvd_dir == direction_f3
    # v4: cvd_30s harus searah dengan cvd_2min (no flip recent)
    short_term_aligned = (cvd > 0 and cvd_30s >= 0) or (cvd < 0 and cvd_30s <= 0)
    cvd_ok = cvd_ok and short_term_aligned

    result.f4 = (
        f"2m={cvd:+,.0f} 30s={cvd_30s:+,.0f} "
        f"{'✅' if cvd_ok else '❌'} (need {direction_f3}, ≥${config.F4_CVD_MIN:,.0f})"
    )
    if not cvd_ok:
        result.skip_reason = f"CVD misaligned ({cvd:+,.0f}, need {direction_f3})"
        return result

    # ── F5: Odds Sanity Check ───────────────────────────────
    odds = snap["odds_up"] if direction_f3 == "UP" else snap["odds_down"]
    odds_ok = config.F5_ODDS_MIN <= odds <= config.F5_ODDS_MAX
    result.f5 = (
        f"odds {direction_f3}={odds:.3f} "
        f"{'✅' if odds_ok else '❌'} "
        f"(range {config.F5_ODDS_MIN:.2f}-{config.F5_ODDS_MAX:.2f})"
    )
    if not odds_ok:
        result.skip_reason = f"Odds {odds:.3f} di luar range RR sehat"
        return result

    # Direction agreement F2 vs F3
    if direction_f2 != direction_f3:
        result.skip_reason = f"F2 ({direction_f2}) ≠ F3 ({direction_f3})"
        return result
    final_direction = direction_f3

    # ── F6: Gocek Cooldown ──────────────────────────────────
    in_cooldown = now < gocek_freeze_until
    if in_cooldown:
        remaining = int(gocek_freeze_until - now)
        result.f6 = f"❌ cooldown {remaining}s"
        result.skip_reason = f"Gocek cooldown ({remaining}s)"
        return result
    result.f6 = "✅ no cooldown"

    # ── F8: Composite Signal Score ──────────────────────────
    # Komponen 0..1, weighted sum
    s_dist = _normalize(abs_dist, config.F2_BEAT_DIST_MIN, cap=3.0)
    s_liq3 = _normalize(recent,   config.F3_LIQ_3S_MIN,    cap=3.0)
    s_liq30= _normalize(sustained,config.F3_LIQ_30S_MIN,   cap=3.0)
    s_cvd  = _normalize(abs(cvd), config.F4_CVD_MIN,       cap=3.0)

    # OBI bonus: +0..0.5 jika OBI searah dengan direction
    obi = snap["obi"]
    obi_aligned = (obi > 0 and final_direction == "UP") or (obi < 0 and final_direction == "DOWN")
    s_obi = min(abs(obi), 1.0) if obi_aligned else 0.0

    # Odds bonus: lebih dekat ke 0.5 = lebih murah, lebih bagus RR
    s_odds = 1.0 - 2 * abs(odds - 0.5)  # 0.5 → 1.0, ekstrem → 0
    s_odds = max(0.0, s_odds)

    # Bobot komponen
    weights = {
        "dist":   0.20,
        "liq3":   0.20,
        "liq30":  0.15,
        "cvd":    0.20,
        "obi":    0.10,
        "odds":   0.15,
    }
    components = {
        "dist":  s_dist, "liq3":  s_liq3,  "liq30": s_liq30,
        "cvd":   s_cvd,  "obi":   s_obi,   "odds":  s_odds,
    }
    score = sum(weights[k] * components[k] for k in weights)
    result.components = components
    result.score = score

    score_ok = score >= config.F8_MIN_SIGNAL_SCORE
    result.f8 = (
        f"score={score:.2f} "
        f"{'✅' if score_ok else '❌'} "
        f"(min {config.F8_MIN_SIGNAL_SCORE:.2f})"
    )

    if not score_ok:
        result.skip_reason = f"Score {score:.2f} < {config.F8_MIN_SIGNAL_SCORE}"
        return result

    # ── ALL PASS ─────────────────────────────────────────────
    result.passed    = True
    result.direction = final_direction
    return result
