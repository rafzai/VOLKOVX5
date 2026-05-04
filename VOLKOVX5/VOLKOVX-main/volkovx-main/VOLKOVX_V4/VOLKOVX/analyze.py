"""
analyze.py — Analisis hasil trading VOLKOVX v4

Improvements over v3:
  - Statistik per direction, per skor bin
  - Sharpe ratio sederhana
  - Max drawdown, win streak
  - Skip-reason kategorization
  - Equity curve summary
"""
import csv
import sys
from pathlib import Path
from collections import defaultdict
from statistics import mean, stdev

LOG_DIR = Path(__file__).parent / "logs"
RESULTS_CSV = LOG_DIR / "volkovx_results.csv"
BLOCKED_CSV = LOG_DIR / "volkovx_blocked.csv"
EQUITY_CSV  = LOG_DIR / "volkovx_equity.csv"


def _to_float(s, default=0.0):
    try:
        return float(s) if s not in ("", None) else default
    except (ValueError, TypeError):
        return default


def _hr(c="="):
    print(c * 60)


def analyze_results():
    if not RESULTS_CSV.exists():
        print("logs/volkovx_results.csv belum ada. Jalankan bot dulu.")
        return

    bets = []
    with open(RESULTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("result") in ("win", "loss"):
                bets.append(row)

    if not bets:
        print("Belum ada bet yang resolved.")
        return

    wins   = [b for b in bets if b["result"] == "win"]
    losses = [b for b in bets if b["result"] == "loss"]
    total_pnl = sum(_to_float(b.get("pnl")) for b in bets)
    pnl_list  = [_to_float(b.get("pnl")) for b in bets]

    _hr()
    print("  VOLKOVX v4 — Analisis Hasil Trading")
    _hr()
    print(f"  Total bet  : {len(bets)}")
    print(f"  WIN        : {len(wins)} ({len(wins)/len(bets)*100:.1f}%)")
    print(f"  LOSS       : {len(losses)} ({len(losses)/len(bets)*100:.1f}%)")
    print(f"  Total PnL  : ${total_pnl:+.2f} USDC")
    print(f"  Avg PnL    : ${total_pnl/len(bets):+.2f} per bet")

    if wins:
        avg_win = mean(_to_float(b.get("pnl")) for b in wins)
        print(f"  Avg WIN    : ${avg_win:+.2f}")
    if losses:
        avg_loss = mean(_to_float(b.get("pnl")) for b in losses)
        print(f"  Avg LOSS   : ${avg_loss:+.2f}")
    if wins and losses:
        rr = abs(avg_win / avg_loss)
        print(f"  Win/Loss   : {rr:.2f}x")

    # Sharpe-ish ratio (naive: mean / stdev * sqrt(N))
    if len(pnl_list) > 1:
        sd = stdev(pnl_list)
        sharpe = (mean(pnl_list) / sd) if sd > 0 else 0
        print(f"  Sharpe~    : {sharpe:.2f}")

    # Max consecutive win/loss streak
    max_w = max_l = cur_w = cur_l = 0
    for b in bets:
        if b["result"] == "win":
            cur_w += 1; cur_l = 0
            max_w = max(max_w, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_l = max(max_l, cur_l)
    print(f"  Max W stk  : {max_w}")
    print(f"  Max L stk  : {max_l}")

    # ── Per direction ────────────────────────────────────────
    by_dir = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for b in bets:
        d   = b.get("direction", "?")
        pnl = _to_float(b.get("pnl"))
        if b["result"] == "win":
            by_dir[d]["w"] += 1
        else:
            by_dir[d]["l"] += 1
        by_dir[d]["pnl"] += pnl

    print("\n  Per Direction:")
    for d, v in by_dir.items():
        total_d = v["w"] + v["l"]
        wr = v["w"] / total_d * 100 if total_d else 0
        print(f"    {d:5s}: {v['w']}W/{v['l']}L ({wr:.1f}%) PnL=${v['pnl']:+.2f}")

    # ── Per signal score bin ─────────────────────────────────
    score_bins = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for b in bets:
        score = _to_float(b.get("score"))
        bin_key = f"{int(score*10)/10:.1f}-{int(score*10)/10 + 0.1:.1f}"
        pnl = _to_float(b.get("pnl"))
        if b["result"] == "win":
            score_bins[bin_key]["w"] += 1
        else:
            score_bins[bin_key]["l"] += 1
        score_bins[bin_key]["pnl"] += pnl

    if score_bins:
        print("\n  Per Score Bin:")
        for k in sorted(score_bins.keys()):
            v = score_bins[k]
            total_d = v["w"] + v["l"]
            wr = v["w"] / total_d * 100 if total_d else 0
            print(f"    {k}: {v['w']}W/{v['l']}L ({wr:.1f}%) PnL=${v['pnl']:+.2f}")

    # ── Blocked breakdown ────────────────────────────────────
    if BLOCKED_CSV.exists():
        with open(BLOCKED_CSV, encoding="utf-8") as f:
            blocked = list(csv.DictReader(f))
        print(f"\n  Blocked signals: {len(blocked)}")
        reasons = defaultdict(int)
        for b in blocked:
            # Extract reason kind (sebelum colon)
            r = b.get("skip_reason", "?").split(":")[0].split("(")[0].strip()
            reasons[r] += 1
        print("  Top skip reasons:")
        for r, cnt in sorted(reasons.items(), key=lambda x: -x[1])[:8]:
            pct = cnt / len(blocked) * 100 if blocked else 0
            print(f"    {cnt:5d}x ({pct:4.1f}%)  {r}")

    # ── Equity curve summary ─────────────────────────────────
    if EQUITY_CSV.exists():
        with open(EQUITY_CSV, encoding="utf-8") as f:
            eq = list(csv.DictReader(f))
        if eq:
            balances = [_to_float(r.get("balance")) for r in eq]
            peak   = max(balances)
            trough = min(balances)
            current = balances[-1]
            print(f"\n  Equity curve:")
            print(f"    Peak    : ${peak:.2f}")
            print(f"    Current : ${current:.2f}")
            print(f"    Drawdown: ${peak - current:.2f} ({(peak-current)/peak*100 if peak > 0 else 0:.1f}%)")

    _hr()


if __name__ == "__main__":
    analyze_results()
