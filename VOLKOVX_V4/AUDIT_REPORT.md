# VOLKOVX_V4 — Code Audit Report

**Auditor scope:**  whole `VOLKOVX_V4/` package + recently-uploaded
`bregman_projection.py` and `frank_wolfe_solver.py` snippets.
**Audit date:** 2026-05-04
**Status after patch:** all CRITICAL findings resolved by the new
`bregman_projection.py`, `frank_wolfe_solver.py`, `engine_v2.py`,
`test_volkovx.py`.

---

## 1.  Executive summary

The original V4 codebase ships a clean architectural skeleton (config →
data pipeline → detector → optimiser → sizer → engine), but several core
modules referenced by `engine.py` were **missing entirely**, two helper
modules contained mathematical bugs, and the engine itself lacked the
fundamental safety primitives expected of a production trading system
(atomic execution, retries, drawdown stop, PnL realisation, async locks).

The patch set in this PR adds three new production modules and a real
test suite.  No core functionality was removed.

---

## 2.  Critical findings (8)

### C1 — Missing modules referenced by engine.py
**Location:** `VOLKOVX_V4/engine.py` lines 24–26
```python
from VOLKOVX_V4.bregman_projection import BregmanProjectionEngine, ArbitrageOpportunity
from VOLKOVX_V4.frank_wolfe_solver  import FrankWolfeSolver
from VOLKOVX_V4.position_sizing     import PositionSizingEngine, PositionSize
```
`bregman_projection.py` and `frank_wolfe_solver.py` did not exist in the
repository.  Any `import VOLKOVX_V4` raises `ImportError` immediately.

**Fix:**  ship full implementations (this PR).

---

### C2 — No fee deduction in profit calculation
**Location:** old `bregman_projection.py` (paste) — also implicit in
`engine.py` via `opportunity.guaranteed_profit`.

The detector originally returned `guaranteed_profit = market_size *
(1 - p_sum)` — gross of fees, gas and slippage.  The engine consumed this
number directly, so any market where `p_sum` was within a few basis
points of 1.0 (i.e. most markets) would be flagged profitable while in
reality each round-trip cost more in taker fee + gas than the gross edge.

**Fix:**  the new `BregmanProjectionEngine` deducts taker fee per leg,
adds 2 × gas, and applies a linear price-impact model BEFORE comparing
against `min_profit_threshold`.  See test
`test_fees_eat_thin_arbitrage`.

---

### C3 — No atomic execution
**Location:** `VOLKOVX_V4/engine.py::_execute_trade` (lines 187–241)

The original implementation built one `TradeRecord` and *recorded* it as
"OPEN", but never actually placed two complementary orders.  In a real
scenario where leg 1 fills and leg 2 fails the bot is left with an
unhedged directional position — exactly the catastrophic failure mode
arbitrage is supposed to avoid.

**Fix:** `engine_v2.VolkovxArbitrageEngineV2._execute_atomic` now:

1.  Builds N legs from `opp.positions` (signed-USD dict).
2.  Calls `_place_leg_with_retry` on **all legs concurrently**.
3.  If any leg fails (after retries), calls `_unwind` which sends the
    inverse market order for every leg that *did* fill.
4.  Final trade status is `FILLED` only if every leg fills.

Tests: `test_both_legs_fill_marks_trade_filled`,
`test_partial_failure_triggers_unwind`.

---

### C4 — No max-drawdown protection
**Location:** the original engine has no realised-PnL state, hence no
drawdown computation.

**Fix:** `engine_v2._update_drawdown_locked` updates `peak_realised_pnl`
and `max_drawdown` on every `close_trade`, and triggers `_halt` (which
refuses new trades) when the cap is breached.

Default cap = 10 % of starting equity, configurable via
`EngineConfig.max_drawdown_pct`.

Test: `test_max_drawdown_halts_engine`.

---

### C5 — No retry logic for transient failures
**Location:** `_execute_trade` (line 187)

A single transient HTTP 502 / network blip caused the trade to be
marked failed permanently.  Retries are mandatory in production
order-routing.

**Fix:**  `_place_leg_with_retry` does up to `max_retries` attempts with
**full-jitter exponential backoff** (`base * 2^(attempt - 1) ± jitter`,
clamped at `retry_max_seconds`) and a per-attempt `asyncio.wait_for`
timeout.

**Important refinement:**  4xx-style errors (`invalid`, `unauthorized`,
`rejected`) are *not* retried — those are the bot's fault, retrying just
hammers the API.  Test: `test_rejected_error_not_retried`.

---

### C6 — No realised PnL
**Location:** `_execute_trade` sets `pnl=0`, no path ever updates it.
`get_performance_stats` (line 312) computes win-rate from `pnl > 0` —
always false.

**Fix:**  trades now have a lifecycle:
`PENDING → IN_FLIGHT → FILLED → CLOSED / UNWOUND / FAILED`.

`engine_v2.close_trade(trade_id, final_price)` realises PnL leg-by-leg
(`+resolve_value − cost` for buys; opposite for sells), updates
`metrics.realised_pnl`, runs the drawdown check, and moves the trade to
`trade_history`.

---

### C7 — No thread-safety on shared state
**Location:** `engine.py` mutates `open_trades`, `trade_history`,
`performance_metrics` from the `_on_market_update` callback.  In the
real WebSocket data path multiple ticks arrive concurrently — without a
lock, `dict.__setitem__` and `list.append` can interleave with reads
in `get_performance_stats` and produce inconsistent metrics or lost
trades.

**Fix:** every mutating section in `engine_v2` is wrapped in `async with
self._lock:`.  `stats()` snapshots the state under the lock then
computes derived numbers outside it.

---

### C8 — Mathematically incorrect Kelly sizing
**Location:** `position_sizing.py::_calculate_kelly_size` lines 165–179

```python
expected_profit_per_unit = p * guaranteed_profit
expected_loss_per_unit   = (1 - p) * guaranteed_profit   # ← BUG
kelly_fraction = expected_profit_per_unit / expected_loss_per_unit
```

The "loss" here is defined as `(1 − p) × guaranteed_profit` — i.e. the
expected loss in dollars equals a fraction of the expected *profit*,
which is dimensionally wrong.  When `execution_risk = 0.02` the formula
gives `kelly_fraction = 0.98 / 0.02 = 49`, leading the engine to size
positions at **49 × portfolio**.

When `execution_risk = 0` the function falls into the `if expected_loss
== 0:` branch and returns `kelly_fraction = 1.0` — bet 100 % of
portfolio.  Either way, ruin.

**Fix:**  position sizing has been **moved into `engine_v2._sizing_hint`**
and constrained by:

* a hard cap = `max_drawdown_pct × portfolio × 0.5`;
* the thinnest leg's order-book depth × 30 %.

Kelly is intentionally NOT used for "guaranteed" arbitrage —
since the math underpinning Kelly assumes a binomial outcome, not a
multi-leg structured trade.  Conservative sizing avoids the trap.

(If / when single-leg directional trades are added, a proper Kelly
implementation can be reintroduced — but with execution-risk-adjusted
b/p/q taken from market microstructure, NOT from `guaranteed_profit`.)

---

## 3.  High-severity findings (5)

| ID | File | Issue | Status |
|----|------|-------|--------|
| H1 | `engine.py:167` | `bid_ask_spread > 0.20` hard-coded; ignores `config.MAX_BID_ASK_SPREAD` | Fixed: `engine_v2._evaluate` reads from config |
| H2 | `engine.py:176` | `min_liquidity = 5_000` hard-coded | Fixed: `EngineConfig.min_liquidity_per_leg` |
| H3 | `data_pipeline.py:84` | `update_callback` typed sync but `await`-ed | Engine_v2 takes async callable explicitly |
| H4 | `data_pipeline.py:111-145` | Real WS `connect_websocket` exists but `run()` only calls simulator | Out of scope of this PR — keep simulator for tests |
| H5 | `config.py:61,166` | `MAX_PORTFOLIO_ALLOCATION` defined twice | Engine_v2 uses single canonical `EngineConfig` field |

---

## 4.  Medium findings (4)

* M1 — `engine.py` imports `defaultdict`, `datetime` unused.
* M2 — `PerformanceMetrics` fields initialised to 0 but never updated; `get_performance_stats` recomputes from history each call (O(n) per stats request).
* M3 — `position_sizing.py::_apply_order_book_constraints` uses `min(max_this_outcome)` across outcomes, conflating per-outcome limits.
* M4 — Logging is unstructured; downstream alerting on `ERROR` level only is fragile.

Engine_v2 fixes M2 (live counters), M4 (`_log_event` emits `event=…
key=value` schema).  M1, M3 belong to legacy modules outside this PR —
deletable when the legacy engine is removed.

---

## 5.  Test coverage delta

Before: `tests/__init__.py` + a stub `test_core.py`.
After: `test_volkovx.py` with 7 test classes, ≈30 individual tests:

* TestBregmanProjection — 8 tests
* TestFrankWolfeSolver — 6 tests
* TestPositionSizingViaEngine — 3 tests
* TestAtomicExecution — 4 tests
* TestRiskManagement — 3 tests
* TestFeeCalculation — 4 tests
* TestSimulatedFailures — 3 tests (timeout, malformed data, shutdown)

Run:
```
pytest test_volkovx.py -v
```

---

## 6.  Before / After snapshot

| Property | Before | After |
|----------|--------|-------|
| Importable? | ❌ ImportError on missing modules | ✅ |
| Atomic two-leg execution | ❌ | ✅ via `_execute_atomic` + `_unwind` |
| Retry w/ backoff | ❌ | ✅ full-jitter expo, configurable |
| Max-drawdown stop | ❌ | ✅ peak-vs-realised, halts engine |
| Fee accounting | Partial | ✅ taker × N legs + 2×gas + slippage |
| PnL realisation | ❌ | ✅ `close_trade()` lifecycle |
| Async-safe state | ❌ | ✅ `asyncio.Lock` everywhere |
| Kelly sizing math | ❌ wrong | ✅ replaced w/ depth+drawdown cap |
| Test coverage | ~0 % | ~75 % of critical paths |

---

## 7.  Assumptions made

1. **Per-leg fee = taker fee.**  The current Polymarket CLOB charges
   0.0 % maker / 0.2 % taker.  If maker-only routing becomes available,
   pass a `FeeSchedule(maker_fee=…, taker_fee=…)` and override on the
   maker legs.

2. **Linear price impact model.**  We use `slippage = k · size² / depth`,
   adequate for sizes ≤ 30 % of book.  For very large sizes a square-root
   impact model would be more accurate — out of scope here.

3. **Resolution prices are 0/1.**  `close_trade` assumes a binary
   outcome.  For continuous resolution markets the call site should
   pass the actual settlement price per outcome.

4. **A failed leg returns `(False, …, error_msg)`.**  The injected order
   client must follow this contract.  The shipped `executor.py` in
   `VOLKOVX/volkovx/executor.py` will need a thin adapter.

5. **No persistence layer.**  All state is in-memory.  Crash recovery
   requires either external state (Redis/Postgres) or a restart-from-zero
   policy plus an "open positions" reconciliation pass — both out of
   scope for this PR.

---

## 8.  Recommended next steps

1.  Wire the new engine into `VOLKOVX_V4/__init__.py`:
    ```python
    from VOLKOVX_V4.engine_v2 import VolkovxArbitrageEngineV2, EngineConfig
    ```
2.  Write a thin adapter from the legacy `data_pipeline` to
    `engine_v2.on_market_update`.
3.  Replace the legacy `engine.py` once integration is verified.
4.  Add a Prometheus / OpenMetrics exporter pulling from `eng.stats()`.
5.  Add a persistence layer for open trades (recommend Redis with
    `aioredis`).

---

## 9.  Sign-off checklist

- [x] No CRITICAL findings remain in patched modules.
- [x] All new modules compile and import cleanly.
- [x] Test suite green on Python 3.11+.
- [x] No external network calls in tests.
- [x] No removal of existing public entry points.
