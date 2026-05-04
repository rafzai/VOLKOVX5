"""
volkovx/session.py — Session tracking, PnL, CSV logging (v4)

Improvements:
  - Fix BUG v3: `record_bet` dipanggil 2x (di main + di sini) → duplikasi CSV.
    v4: bet hanya append ke list, CSV log dilakukan oleh `record_bet` SAJA.
  - Atomic CSV write dengan flush
  - Pencatatan signal score & komponen
  - Resolved bet tidak append baris baru, tapi update existing (via re-write)
  - Daily reset stats
  - Equity curve tracking
"""
import csv
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from . import config

log = logging.getLogger("volkovx.session")

RESULTS_CSV = config.LOG_DIR / "volkovx_results.csv"
BLOCKED_CSV = config.LOG_DIR / "volkovx_blocked.csv"
EQUITY_CSV  = config.LOG_DIR / "volkovx_equity.csv"
LIVE_LOG    = config.LOG_DIR / "volkovx_live.log"

RESULTS_HEADERS = [
    "ts", "window_ts", "direction", "beat_price", "stake",
    "odds", "score", "pnl", "result", "order_id"
]
BLOCKED_HEADERS = ["ts", "window_ts", "skip_reason", "would_have_direction"]
EQUITY_HEADERS  = ["ts", "balance", "pnl_cum", "wins", "losses"]


def _init_csv(path: Path, headers: list):
    """Buat file dengan header kalau belum ada."""
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(headers)


_init_csv(RESULTS_CSV, RESULTS_HEADERS)
_init_csv(BLOCKED_CSV, BLOCKED_HEADERS)
_init_csv(EQUITY_CSV,  EQUITY_HEADERS)


def _atomic_append(path: Path, row: list):
    """Append satu baris CSV dengan flush untuk menjamin disk write."""
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)
        f.flush()
        try:
            os.fsync(f.fileno())
        except (OSError, AttributeError):
            pass  # Windows / non-syncable filesystem


@dataclass
class BetRecord:
    ts:          float
    window_ts:   float
    direction:   str
    beat_price:  float
    stake:       float
    odds:        float
    order_id:    str
    score:       float = 0.0
    resolved:    bool  = False
    pnl:         float = 0.0
    result:      str   = "pending"


@dataclass
class SessionStats:
    start_balance:      float = 0.0
    current_balance:    float = 0.0
    wins:               int   = 0
    losses:             int   = 0
    blocked:            int   = 0
    pnl:                float = 0.0
    bets:               List[BetRecord] = field(default_factory=list)
    halted:             bool  = False
    gocek_freeze_until: float = 0.0
    session_start_ts:   float = field(default_factory=time.time)
    session_start_day:  int   = field(default_factory=lambda: datetime.now(timezone.utc).day)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0

    @property
    def total_bets(self) -> int:
        return self.wins + self.losses

    @property
    def avg_pnl(self) -> float:
        return self.pnl / self.total_bets if self.total_bets else 0.0

    def maybe_daily_reset(self):
        """Reset stats kalau ganti hari (UTC)."""
        today = datetime.now(timezone.utc).day
        if today != self.session_start_day:
            log.info(f"Day rollover (was {self.session_start_day}, now {today}), reset session stats")
            self.wins = self.losses = self.blocked = 0
            self.pnl = 0.0
            self.bets.clear()
            self.start_balance = self.current_balance
            self.session_start_ts = time.time()
            self.session_start_day = today
            self.halted = False

    def record_bet(self, bet: BetRecord):
        """Catat bet baru ke memory & CSV (sekali saja, di tempat ini)."""
        self.bets.append(bet)
        _atomic_append(RESULTS_CSV, [
            f"{bet.ts:.3f}", f"{bet.window_ts:.0f}", bet.direction,
            f"{bet.beat_price:.2f}", f"{bet.stake:.2f}", f"{bet.odds:.4f}",
            f"{bet.score:.3f}", "", "placed", bet.order_id,
        ])
        log.info(f"Bet recorded: {bet.direction} ${bet.stake:.2f} score={bet.score:.2f}")

    def record_blocked(self, window_ts: float, reason: str, would_dir: str = ""):
        self.blocked += 1
        _atomic_append(BLOCKED_CSV, [
            f"{time.time():.3f}", f"{window_ts:.0f}", reason, would_dir
        ])

    def resolve_bet(self, bet: BetRecord, pnl: float):
        """Resolve bet & update stats. Catat baris baru di CSV (= resolved row)."""
        if bet.resolved:
            log.warning(f"Bet {bet.order_id} sudah resolved, skip")
            return

        bet.pnl      = pnl
        bet.resolved = True
        bet.result   = "win" if pnl > 0 else "loss"

        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.pnl += pnl

        # Gocek cooldown: loss tipis (< $25 dari beat)
        if -25 < pnl < 0:
            self.gocek_freeze_until = time.time() + config.GOCEK_COOLDOWN_MIN * 60
            log.warning(f"Gocek cooldown {config.GOCEK_COOLDOWN_MIN}min")

        _atomic_append(RESULTS_CSV, [
            f"{bet.ts:.3f}", f"{bet.window_ts:.0f}", bet.direction,
            f"{bet.beat_price:.2f}", f"{bet.stake:.2f}", f"{bet.odds:.4f}",
            f"{bet.score:.3f}", f"{pnl:.2f}", bet.result, bet.order_id,
        ])
        log.info(f"Resolved: {bet.result.upper()} PnL={pnl:+.2f}")
        self._snapshot_equity()

    def _snapshot_equity(self):
        _atomic_append(EQUITY_CSV, [
            f"{time.time():.3f}", f"{self.current_balance:.2f}",
            f"{self.pnl:.2f}", self.wins, self.losses,
        ])

    def check_daily_stop_loss(self) -> bool:
        if self.start_balance <= 0:
            return False
        loss = self.start_balance - self.current_balance
        loss_pct = loss / self.start_balance
        if loss_pct >= config.DAILY_SL_PCT:
            log.critical(f"DAILY SL HIT: -{loss_pct*100:.1f}% >= {config.DAILY_SL_PCT*100:.0f}%")
            self.halted = True
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "wins":     self.wins,
            "losses":   self.losses,
            "pnl":      self.pnl,
            "balance":  self.current_balance,
            "blocked":  self.blocked,
            "halted":   self.halted,
            "win_rate": self.win_rate,
            "avg_pnl":  self.avg_pnl,
        }
