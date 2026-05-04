# VOLKOVX V4 — Deployment Guide

End-to-end checklist for taking the patched arbitrage engine from local
clone to production trading.  Three rollout stages: **DRY**, **CANARY**,
**LIVE**.

---

## 0.  Prerequisites

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python     | ≥ 3.11  | f-strings, asyncio improvements, dataclass kw_only |
| pip / uv   | latest  | package install |
| git        | ≥ 2.30  | repo & branching |
| (optional) Linux/Mac | — | for `uvloop` (skip on Windows) |

External accounts:

* **Polymarket** wallet (Polygon mainnet) with USDC funded.
* **Polymarket CLOB API** key — generated via `generate_api_creds.py`.
* **Polygon RPC** — Alchemy, QuickNode, or self-hosted.
* (optional) **Relayer** API key for gas-free claim.

---

## 1.  Clone and install

```bash
git clone https://github.com/<YOU>/volkovx.git
cd volkovx/VOLKOVX-main/VOLKOVX-main/VOLKOVX_V4

# Recommended: create venv
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

# Install deps
pip install -r VOLKOVX/requirements.txt
pip install pytest pytest-asyncio    # for the new test suite
```

---

## 2.  Add the new modules

Copy the four patch files into `VOLKOVX_V4/` (sibling to `engine.py`):

```
VOLKOVX_V4/
├── bregman_projection.py        ← NEW
├── frank_wolfe_solver.py        ← NEW
├── engine_v2.py                 ← NEW
├── engine.py                    ← legacy, can be deleted later
├── config.py
├── data_pipeline.py
├── position_sizing.py
└── VOLKOVX/
    └── tests/
        └── test_volkovx.py      ← NEW
```

Update `VOLKOVX_V4/__init__.py` to expose the new entry-points:

```python
from VOLKOVX_V4.engine_v2          import VolkovxArbitrageEngineV2, EngineConfig
from VOLKOVX_V4.bregman_projection import (
    ArbitrageOpportunity, BregmanProjectionEngine, FeeSchedule,
)
from VOLKOVX_V4.frank_wolfe_solver import FrankWolfeSolver, MarketAllocationInput

# keep legacy exports for backwards compat:
from VOLKOVX_V4.engine import VolkovxArbitrageEngine
```

---

## 3.  Run the test suite (mandatory)

```bash
cd VOLKOVX_V4
pytest VOLKOVX/tests/test_volkovx.py -v --tb=short
```

Expected output ends with `XX passed in <s>s` and **zero** errors /
failures.  If anything fails, **do not proceed**; open an issue and
attach the failing trace.

To run a single bucket:

```bash
pytest -k atomic -v
pytest -k drawdown -v
```

---

## 4.  Configuration

### 4.1  Environment variables

Create `.env` from the template:

```bash
cp .env.example .env
$EDITOR .env
```

Critical fields:

| Var | Required | Notes |
|-----|----------|-------|
| `POLYMARKET_PRIVATE_KEY` | LIVE only | 0x-prefixed hex, **64 chars** |
| `POLYMARKET_FUNDER`      | LIVE only | 0x address that owns USDC |
| `POLYMARKET_API_KEY` / `_SECRET` / `_PASSPHRASE` | LIVE | from `generate_api_creds.py` |
| `VOLKOVX_DRY_RUN` | LIVE deploy → `false`; CANARY → `true` | |
| `VOLKOVX_STAKE_USD` | always | start at $2 for canary |
| `VOLKOVX_DAILY_SL_PCT` | always | `0.10` = 10 % |

### 4.2  Engine config (programmatic)

```python
from VOLKOVX_V4 import EngineConfig, VolkovxArbitrageEngineV2, FeeSchedule

cfg = EngineConfig(
    portfolio_value          = 10_000.0,
    min_profit_threshold     = 0.50,
    min_liquidity_per_leg    = 5_000.0,
    max_concurrent_positions = 5,
    max_drawdown_pct         = 0.05,      # 5 % HARD STOP
    daily_loss_pct           = 0.05,
    daily_max_trades         = 100,
    max_retries              = 3,
    retry_base_seconds       = 0.25,
    leg_timeout_seconds      = 5.0,
    fee_schedule             = FeeSchedule(
        taker_fee = 0.002,
        gas_usd   = 0.05,
    ),
)
```

---

## 5.  Stage A — DRY (no money)

Goal: verify the WHOLE pipeline (data feed → detection → execution
mock → close → metrics) works end-to-end.

```bash
export VOLKOVX_DRY_RUN=true
python -m VOLKOVX_V4.engine_v2_smoke   # or your top-level launcher
```

Acceptance criteria — let it run **at least 30 minutes**, then check:

* `eng.metrics.arbitrages_detected > 0`
* `eng.metrics.trades_attempted >= 1`
* `eng.metrics.trades_unwound == 0` (no unexpected unwinds in DRY)
* No `ERROR` lines in logs.
* `eng.stats()['halted'] == False`.

---

## 6.  Stage B — CANARY ($2–$5 stake)

Goal: validate real fills, real fees, real slippage at *minimal* size.

1.  Set `VOLKOVX_DRY_RUN=false`, `VOLKOVX_STAKE_USD=2.00`.
2.  Lower the engine cap:
    ```python
    cfg.max_drawdown_pct = 0.02     # 2 % stop
    cfg.daily_max_trades = 20       # small daily count
    ```
3.  Run for **24 hours**.

Acceptance criteria:

* `realised_pnl ± 1$` of expected (i.e. fees and slippage are within
  the modelled envelope).
* `trades_unwound / trades_attempted < 5 %`.
* No leg ever times out more than once per trade.

If `realised_pnl` consistently underperforms `expected_profit`, raise
`taker_fee` and / or `impact_coef` in `FeeSchedule` until they match.

---

## 7.  Stage C — LIVE

Once canary criteria are met:

1.  Bump `VOLKOVX_STAKE_USD` to target.
2.  Tighten `cfg.max_drawdown_pct` based on canary realised volatility
    (rule of thumb: 3× canary daily range).
3.  Enable monitoring:

   ```python
   import asyncio, json
   async def metrics_emitter(eng):
       while True:
           print(json.dumps(eng.stats()))
           await asyncio.sleep(60)
   ```

4.  Hook **two alerts** (Slack / Discord / PagerDuty):
   * `eng.stats()['halted'] == True`
   * `eng.stats()['trades_unwound']` increases > 5 in 5 min.

---

## 8.  Operational runbook

### Engine halted unexpectedly

```python
print(eng.stats()['halt_reason'])
```

Common reasons:

| Reason | Action |
|--------|--------|
| `max_drawdown_breached` | Audit recent trades; check fee model is matching reality.  Resume only after PnL recovers above peak − cap. |
| `daily_loss_breached`   | Wait for next UTC day, or restart with adjusted `daily_loss_pct`. |
| `daily_max_trades`      | Increase `daily_max_trades` if appropriate. |
| `shutdown`              | Operator-initiated; safe to restart. |

### Many `trades_unwound`

```bash
grep "trade_unwound" volkovx.log | tail -20
```

Look at `reason=` field.  If `partial_failure` predominates, the order
client is the culprit — check API rate limits and increase
`retry_base_seconds` / `max_retries`.

### Reconciling open positions after a crash

The engine has no persistence layer.  After restart:

1.  Query Polymarket for open positions on the funder address.
2.  For each position, either:
   * issue a manual closing order, or
   * mark it as a synthetic `Leg.is_filled` and call `close_trade`
     once the market resolves.

Implementing a Redis-backed persistence layer is the next planned PR.

---

## 9.  Rollout checklist

- [ ] All tests pass on the deployment box.
- [ ] `.env` is filled and **never** committed.
- [ ] Monitoring alerts wired (halted, unwound spike).
- [ ] Stage A (DRY) passed 30 min minimum.
- [ ] Stage B (CANARY) passed 24 h minimum.
- [ ] Drawdown cap set ≤ canary observed volatility × 3.
- [ ] `VOLKOVX_STAKE_USD` set conservatively for first day live.
- [ ] Shutdown procedure documented to ops (`SIGTERM` triggers
      `await eng.shutdown()` which unwinds all open trades).

---

## 10.  Rollback

If any of the LIVE acceptance criteria fail:

1.  Send `SIGTERM` → `eng.shutdown()` runs and unwinds all open trades.
2.  Set `VOLKOVX_DRY_RUN=true` and re-run to confirm the issue
    reproduces in DRY.
3.  Revert the deploy via:
    ```bash
    git revert HEAD
    git push origin main
    ```

The legacy `engine.py` is left in-tree precisely so a rollback is
two commits away.
