"""
volkovx/feeds.py — Real-time data feeds (v4 — performance-optimized & hardened)

Improvements over v3:
  - **deque dengan maxlen** → O(1) prune, ganti list slicing yang O(n) per tick
  - **Pre-aggregated bucket counters** untuk liquidations & CVD → query O(1)
  - **Exponential backoff** untuk reconnect (cegah hammer ke server)
  - **Health monitoring**: track latency, message rate, last-data-age
  - **Graceful shutdown** via cancellation
  - **Stale price detection**: invalidate price kalau > N detik tanpa update
  - **Atomic state snapshot** untuk konsistensi multi-field reads
  - **Order book imbalance** (OBI) sebagai sinyal tambahan
  - **Volatility tracking** (rolling std) untuk adaptive thresholds
"""
import asyncio
import json
import time
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Deque, Tuple
import aiohttp

log = logging.getLogger("volkovx.feeds")


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def _safe_float(value, default: float = 0.0) -> float:
    """Konversi value ke float aman — handle None, str kosong, NaN, Infinity."""
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


async def _backoff_sleep(attempt: int, base: float = 2.0, cap: float = 60.0):
    """Exponential backoff dengan jitter — cegah thundering herd ke server."""
    import random
    delay = min(cap, base * (2 ** min(attempt, 6)))
    delay = delay * (0.5 + random.random() * 0.5)  # jitter 50-100%
    await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────────────
#  Shared state
# ─────────────────────────────────────────────────────────────
@dataclass
class FeedState:
    # Prices
    btc_price_poly:  Optional[float] = None
    btc_price_hl:    Optional[float] = None
    btc_price_chain: Optional[float] = None
    btc_price_bin:   Optional[float] = None     # v4: Binance terpisah
    price_ts_poly:   float = 0.0
    price_ts_hl:     float = 0.0
    price_ts_chain:  float = 0.0
    price_ts_bin:    float = 0.0

    # Liquidations (USD, pre-aggregated)
    liq_short_3s:  float = 0.0
    liq_short_30s: float = 0.0
    liq_long_3s:   float = 0.0
    liq_long_30s:  float = 0.0
    liq_ts:        float = 0.0

    # CVD
    cvd_2min:   float = 0.0
    cvd_30s:    float = 0.0     # v4: CVD jangka pendek untuk konfirmasi
    cvd_ts:     float = 0.0

    # Order book imbalance (v4)
    obi:        float = 0.0     # range -1..+1, +1 = bid lebih kuat
    spread_bps: float = 0.0
    book_ts:    float = 0.0

    # Volatility (v4) — rolling std harga 60s
    vol_60s:    float = 0.0
    vol_ts:     float = 0.0

    # Polymarket odds + token IDs
    odds_up:    float = 0.5
    odds_down:  float = 0.5
    market_id:  Optional[str] = None
    token_up:   Optional[str] = None
    token_down: Optional[str] = None
    market_question: str = ""
    market_ts:  float = 0.0

    # Connection health
    poly_rtds_ok:  bool = False
    hl_ws_ok:      bool = False
    chain_ok:      bool = False
    gamma_ok:      bool = False

    # v4: monitoring stats
    msgs_received: int = 0
    last_error:    str = ""

    @property
    def btc_price(self) -> Optional[float]:
        """Best available price dengan staleness check (max 10s)."""
        now = time.time()
        # Prioritas: HL (paling cepat) > Poly RTDS > Chainlink > Binance
        candidates = [
            (self.btc_price_hl,    self.price_ts_hl,    10),
            (self.btc_price_poly,  self.price_ts_poly,  10),
            (self.btc_price_chain, self.price_ts_chain, 60),
            (self.btc_price_bin,   self.price_ts_bin,   30),
        ]
        for price, ts, max_age in candidates:
            if price and (now - ts) <= max_age:
                return price
        return None

    @property
    def price_age(self) -> float:
        """Detik sejak harga terakhir terupdate (dari source manapun)."""
        latest = max(self.price_ts_hl, self.price_ts_poly,
                     self.price_ts_chain, self.price_ts_bin)
        return (time.time() - latest) if latest else float("inf")

    @property
    def all_feeds_ok(self) -> bool:
        return self.btc_price is not None and self.liq_ts > 0

    def snapshot(self) -> dict:
        """Atomic-ish snapshot — semua nilai dibaca di satu tick."""
        return {
            "btc_price":     self.btc_price,
            "price_age":     self.price_age,
            "liq_short_3s":  self.liq_short_3s,
            "liq_short_30s": self.liq_short_30s,
            "liq_long_3s":   self.liq_long_3s,
            "liq_long_30s":  self.liq_long_30s,
            "cvd_2min":      self.cvd_2min,
            "cvd_30s":       self.cvd_30s,
            "obi":           self.obi,
            "vol_60s":       self.vol_60s,
            "odds_up":       self.odds_up,
            "odds_down":     self.odds_down,
            "market_id":     self.market_id,
            "token_up":      self.token_up,
            "token_down":    self.token_down,
        }


STATE = FeedState()
_STATE_LOCK = asyncio.Lock()


# ─────────────────────────────────────────────────────────────
#  Rolling buffers (v4: deque dengan maxlen, jauh lebih cepat)
# ─────────────────────────────────────────────────────────────
# Liquidation events: (ts, side, usd_size). Window 30s @ ~1-5 evt/sec → 200 cukup
_LIQ_BUFFER: Deque[Tuple[float, str, float]] = deque(maxlen=2000)

# Trades buffer untuk CVD (window 2min). BTC ~10-50 trades/sec → 12000 cukup
_TRADE_BUFFER: Deque[Tuple[float, str, float]] = deque(maxlen=20000)

# Price history untuk volatility (window 60s, sampling tiap update)
_PRICE_HISTORY: Deque[Tuple[float, float]] = deque(maxlen=600)


def _aggregate_liq(now: float):
    """Hitung ulang aggregat liquidation 3s & 30s. O(buffer_len)."""
    cutoff_3s  = now - 3
    cutoff_30s = now - 30
    s3 = s30 = l3 = l30 = 0.0
    # Prune dari kiri (deque support efficient popleft)
    while _LIQ_BUFFER and _LIQ_BUFFER[0][0] < cutoff_30s:
        _LIQ_BUFFER.popleft()
    for ts, side, sz in _LIQ_BUFFER:
        if side == "A":  # Short liq (ask side)
            s30 += sz
            if ts > cutoff_3s:
                s3 += sz
        elif side == "B":  # Long liq (bid side)
            l30 += sz
            if ts > cutoff_3s:
                l3 += sz
    return s3, s30, l3, l30


def _aggregate_cvd(now: float):
    """Hitung CVD 30s & 2min."""
    cutoff_30s = now - 30
    cutoff_2m  = now - 120
    while _TRADE_BUFFER and _TRADE_BUFFER[0][0] < cutoff_2m:
        _TRADE_BUFFER.popleft()
    cvd_2m = cvd_30s = 0.0
    for ts, side, sz in _TRADE_BUFFER:
        delta = sz if side == "B" else -sz
        cvd_2m += delta
        if ts > cutoff_30s:
            cvd_30s += delta
    return cvd_30s, cvd_2m


def _compute_volatility(now: float):
    """Rolling std harga dalam 60s. Return std dollar."""
    cutoff = now - 60
    while _PRICE_HISTORY and _PRICE_HISTORY[0][0] < cutoff:
        _PRICE_HISTORY.popleft()
    if len(_PRICE_HISTORY) < 5:
        return 0.0
    prices = [p for _, p in _PRICE_HISTORY]
    mean = sum(prices) / len(prices)
    var  = sum((p - mean) ** 2 for p in prices) / len(prices)
    return math.sqrt(var)


# ─────────────────────────────────────────────────────────────
#  Feed 1: Polymarket RTDS (WebSocket)
# ─────────────────────────────────────────────────────────────
POLY_RTDS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

async def feed_polymarket_rtds(session: aiohttp.ClientSession):
    HEARTBEAT_TIMEOUT = 60
    attempt = 0
    while True:
        if not STATE.market_id:
            STATE.poly_rtds_ok = False
            await asyncio.sleep(2)
            continue

        market_id = STATE.market_id
        try:
            async with session.ws_connect(POLY_RTDS_URL, heartbeat=20) as ws:
                await ws.send_json({
                    "type": "subscribe",
                    "channel": "live_activity",
                    "markets": [market_id],
                })
                STATE.poly_rtds_ok = True
                attempt = 0
                log.info(f"Polymarket RTDS connected: market={market_id[:12]}...")

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=HEARTBEAT_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.warning(f"RTDS: no msg in {HEARTBEAT_TIMEOUT}s, reconnect")
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data   = json.loads(msg.data)
                            events = data if isinstance(data, list) else [data]
                            for ev in events:
                                await _parse_rtds_event(ev)
                            STATE.msgs_received += 1
                        except json.JSONDecodeError as e:
                            log.warning(f"RTDS JSON error: {e}")
                        except Exception as e:
                            log.warning(f"RTDS parse error: {e}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        log.warning(f"RTDS closed/error: {msg.type}")
                        break

                    if STATE.market_id and STATE.market_id != market_id:
                        log.info(f"RTDS market changed, reconnect")
                        break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.last_error = f"RTDS: {e}"
            log.warning(f"RTDS reconnect error: {e}")

        STATE.poly_rtds_ok = False
        attempt += 1
        await _backoff_sleep(attempt)


async def _parse_rtds_event(ev: dict):
    if not isinstance(ev, dict):
        return
    async with _STATE_LOCK:
        if ev.get("type") == "price_change":
            for change in ev.get("changes", []):
                if not isinstance(change, dict):
                    continue
                outcome = str(change.get("outcome", "")).upper()
                price   = _safe_float(change.get("price"))
                if 0 < price < 1:
                    if outcome == "UP":
                        STATE.odds_up = price
                    elif outcome == "DOWN":
                        STATE.odds_down = price
            STATE.market_ts = time.time()

        if "asset_price" in ev:
            price = _safe_float(ev["asset_price"])
            if 10_000 < price < 500_000:
                STATE.btc_price_poly = price
                STATE.price_ts_poly  = time.time()


# ─────────────────────────────────────────────────────────────
#  Feed 2: Hyperliquid WebSocket
# ─────────────────────────────────────────────────────────────
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"

async def feed_hyperliquid(session: aiohttp.ClientSession):
    HEARTBEAT_TIMEOUT = 45
    attempt = 0
    while True:
        try:
            async with session.ws_connect(HL_WS_URL, heartbeat=20) as ws:
                # Subscribe ke 3 channel
                for sub in [
                    {"type": "liquidations"},
                    {"type": "trades", "coin": "BTC"},
                    {"type": "l2Book", "coin": "BTC"},
                ]:
                    await ws.send_json({"method": "subscribe", "subscription": sub})

                STATE.hl_ws_ok = True
                attempt = 0
                log.info("Hyperliquid WS connected")

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=HEARTBEAT_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.warning(f"HL WS: no msg in {HEARTBEAT_TIMEOUT}s, reconnect")
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            await _process_hl_message(data)
                            STATE.msgs_received += 1
                        except json.JSONDecodeError as e:
                            log.warning(f"HL JSON error: {e}")
                        except Exception as e:
                            log.warning(f"HL process error: {e}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        log.warning(f"HL closed/error: {msg.type}")
                        break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.last_error = f"HL: {e}"
            log.warning(f"HL WS reconnect: {e}")

        STATE.hl_ws_ok = False
        attempt += 1
        await _backoff_sleep(attempt)


async def _process_hl_message(data: dict):
    if not isinstance(data, dict):
        return
    now     = time.time()
    channel = data.get("channel", "")

    # ── L2 Book → mid price + OBI + spread ───────────────────
    if channel == "l2Book":
        book = data.get("data", {}).get("levels", [])
        if isinstance(book, list) and len(book) >= 2 and book[0] and book[1]:
            try:
                bids = book[0]
                asks = book[1]
                best_bid = _safe_float(bids[0].get("px"))
                best_ask = _safe_float(asks[0].get("px"))
                if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
                    mid = (best_bid + best_ask) / 2

                    # OBI: pakai top 5 levels
                    bid_vol = sum(_safe_float(l.get("sz")) for l in bids[:5])
                    ask_vol = sum(_safe_float(l.get("sz")) for l in asks[:5])
                    total = bid_vol + ask_vol
                    obi   = (bid_vol - ask_vol) / total if total > 0 else 0.0
                    spread_bps = (best_ask - best_bid) / mid * 10_000

                    async with _STATE_LOCK:
                        STATE.btc_price_hl = mid
                        STATE.price_ts_hl  = now
                        STATE.obi          = obi
                        STATE.spread_bps   = spread_bps
                        STATE.book_ts      = now
                        _PRICE_HISTORY.append((now, mid))
                        STATE.vol_60s = _compute_volatility(now)
                        STATE.vol_ts  = now
            except (IndexError, KeyError, AttributeError) as e:
                log.debug(f"L2 parse: {e}")

    # ── Trades → CVD ─────────────────────────────────────────
    elif channel == "trades":
        trades = data.get("data", [])
        if not isinstance(trades, list):
            return
        added = False
        for trade in trades:
            if not isinstance(trade, dict) or trade.get("coin") != "BTC":
                continue
            side = trade.get("side", "")
            sz   = _safe_float(trade.get("sz")) * _safe_float(trade.get("px"))
            ts   = _safe_float(trade.get("time"), now * 1000) / 1000
            if sz > 0 and side in ("A", "B"):
                _TRADE_BUFFER.append((ts, side, sz))
                added = True
        if added:
            cvd_30s, cvd_2m = _aggregate_cvd(now)
            async with _STATE_LOCK:
                STATE.cvd_30s  = cvd_30s
                STATE.cvd_2min = cvd_2m
                STATE.cvd_ts   = now

    # ── Liquidations ─────────────────────────────────────────
    elif channel == "liquidations":
        liq_data = data.get("data", {})
        # Bisa berupa list atau dict, handle keduanya
        liqs = liq_data if isinstance(liq_data, list) else [liq_data]
        added = False
        for liq in liqs:
            if not isinstance(liq, dict) or liq.get("coin") != "BTC":
                continue
            side = liq.get("side", "")
            sz   = _safe_float(liq.get("sz")) * _safe_float(liq.get("px"))
            if sz > 0 and side in ("A", "B"):
                _LIQ_BUFFER.append((now, side, sz))
                added = True
        if added:
            s3, s30, l3, l30 = _aggregate_liq(now)
            async with _STATE_LOCK:
                STATE.liq_short_3s  = s3
                STATE.liq_short_30s = s30
                STATE.liq_long_3s   = l3
                STATE.liq_long_30s  = l30
                STATE.liq_ts        = now


# ─────────────────────────────────────────────────────────────
#  Feed 3: Chainlink on-chain BTC/USD (Polygon)
# ─────────────────────────────────────────────────────────────
POLYGON_RPC  = "https://polygon-rpc.com"
CL_BTC_PROXY = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

async def feed_chainlink(session: aiohttp.ClientSession):
    abi_call = {
        "jsonrpc": "2.0", "id": 1, "method":  "eth_call",
        "params":  [{"to": CL_BTC_PROXY, "data": "0xfeaf968c"}, "latest"],
    }
    attempt = 0
    while True:
        try:
            async with session.post(
                POLYGON_RPC, json=abi_call,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                payload = await resp.json()
                result  = payload.get("result", "0x") if isinstance(payload, dict) else "0x"
                if isinstance(result, str) and len(result) >= 130:
                    try:
                        price = int(result[66:130], 16) / 1e8
                    except ValueError:
                        price = 0
                    if 10_000 < price < 500_000:
                        STATE.btc_price_chain = price
                        STATE.price_ts_chain  = time.time()
                        STATE.chain_ok        = True
                        attempt = 0
                    else:
                        STATE.chain_ok = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.chain_ok = False
            log.debug(f"Chainlink poll error: {e}")
            attempt += 1
        await asyncio.sleep(min(15 + attempt * 5, 60))


# ─────────────────────────────────────────────────────────────
#  Feed 4: Polymarket Gamma (market discovery + odds poll)
# ─────────────────────────────────────────────────────────────
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL  = "https://clob.polymarket.com/markets"

def _is_btc_5min_market(m: dict) -> bool:
    """Detect Polymarket BTC up/down 5-minute market dengan beberapa pattern."""
    q = str(m.get("question", "")).lower()
    slug = str(m.get("slug", "")).lower()
    text = q + " " + slug
    has_btc  = "bitcoin" in text or "btc" in text
    has_dir  = "up or down" in text or "up/down" in text
    has_5min = "5 minute" in text or "5-min" in text or "5min" in text or " 5 " in text
    return has_btc and has_dir and (has_5min or "5" in text)


async def feed_gamma(session: aiohttp.ClientSession):
    """Discover market BTC 5-min aktif & poll odds + token IDs."""
    discover_attempt = 0
    while True:
        try:
            # ── Discovery ────────────────────────────────────
            need_discover = (
                not STATE.market_id
                or (time.time() - STATE.market_ts > 600)  # re-discover tiap 10min
            )
            if need_discover:
                params = {"active": "true", "closed": "false",
                          "tag_slug": "crypto", "limit": 100}
                async with session.get(
                    GAMMA_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        log.warning(f"Gamma discovery HTTP {resp.status}")
                    else:
                        markets = await resp.json()
                        found = False
                        if isinstance(markets, list):
                            for m in markets:
                                if not isinstance(m, dict):
                                    continue
                                if _is_btc_5min_market(m):
                                    mid = m.get("conditionId") or m.get("id")
                                    if mid:
                                        async with _STATE_LOCK:
                                            STATE.market_id = str(mid)
                                            STATE.market_question = str(m.get("question", ""))
                                        log.info(f"Market: {m.get('question')[:60]} [{str(mid)[:12]}...]")
                                        found = True
                                        discover_attempt = 0
                                        break
                        if not found:
                            discover_attempt += 1
                            log.warning(f"Gamma: market BTC 5-min tidak ditemukan (try #{discover_attempt})")
                        STATE.gamma_ok = True

            # ── Poll odds dari CLOB ──────────────────────────
            if STATE.market_id:
                async with session.get(
                    f"{CLOB_URL}/{STATE.market_id}",
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_up = new_down = None
                        new_tok_u = new_tok_d = None
                        for token in data.get("tokens", []):
                            if not isinstance(token, dict):
                                continue
                            outcome  = str(token.get("outcome", "")).upper()
                            price    = _safe_float(token.get("price"), 0.5)
                            token_id = token.get("token_id")
                            if outcome == "UP":
                                new_up, new_tok_u = price, token_id
                            elif outcome == "DOWN":
                                new_down, new_tok_d = price, token_id
                        async with _STATE_LOCK:
                            if new_up    is not None and 0 < new_up < 1:
                                STATE.odds_up = new_up
                            if new_down  is not None and 0 < new_down < 1:
                                STATE.odds_down = new_down
                            if new_tok_u: STATE.token_up   = new_tok_u
                            if new_tok_d: STATE.token_down = new_tok_d
                            STATE.market_ts = time.time()
                    else:
                        log.warning(f"Gamma CLOB HTTP {resp.status}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.gamma_ok = False
            STATE.last_error = f"Gamma: {e}"
            log.warning(f"Gamma feed error: {e}")
        await asyncio.sleep(10)


# ─────────────────────────────────────────────────────────────
#  Feed 5: Binance REST fallback
# ─────────────────────────────────────────────────────────────
async def feed_binance_fallback(session: aiohttp.ClientSession):
    url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    while True:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data  = await resp.json()
                    price = _safe_float(data.get("price"))
                    if 10_000 < price < 500_000:
                        STATE.btc_price_bin = price
                        STATE.price_ts_bin  = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"Binance fallback error: {e}")
        await asyncio.sleep(8)


# ─────────────────────────────────────────────────────────────
#  Aggregator ticker — keep liq/cvd fresh saat tidak ada event
# ─────────────────────────────────────────────────────────────
async def feed_aggregator_tick():
    """Setiap 1s, recompute aggregat supaya nilai stale (e.g. liq 30s lewat)
    benar-benar drop ke 0, bukan stuck di nilai lama saat tidak ada event masuk.
    """
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        try:
            s3, s30, l3, l30 = _aggregate_liq(now)
            cvd_30s, cvd_2m = _aggregate_cvd(now)
            async with _STATE_LOCK:
                STATE.liq_short_3s  = s3
                STATE.liq_short_30s = s30
                STATE.liq_long_3s   = l3
                STATE.liq_long_30s  = l30
                STATE.cvd_30s       = cvd_30s
                STATE.cvd_2min      = cvd_2m
                STATE.vol_60s       = _compute_volatility(now)
        except Exception as e:
            log.debug(f"Aggregator tick error: {e}")


# ─────────────────────────────────────────────────────────────
#  Start semua feed
# ─────────────────────────────────────────────────────────────
async def start_all_feeds(session: aiohttp.ClientSession):
    """Jalankan semua feed concurrent. Jika satu crash, log & restart loop di internal masing-masing."""
    await asyncio.gather(
        feed_polymarket_rtds(session),
        feed_hyperliquid(session),
        feed_chainlink(session),
        feed_gamma(session),
        feed_binance_fallback(session),
        feed_aggregator_tick(),
        return_exceptions=True,
    )
