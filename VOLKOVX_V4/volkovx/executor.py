"""
volkovx/executor.py — Place & monitor orders via Polymarket CLOB (v4)

Improvements:
  - Retry dengan exponential backoff untuk transient errors (5xx, network)
  - Idempotency key UUID — cegah double-bet saat retry
  - Validasi token_id, price, size sebelum kirim
  - Slippage protection: limit price = best odds + tolerance
  - Balance cache (TTL 5s) untuk kurangi API call
  - Safer error handling — bedakan transient vs permanent error
  - Order monitoring: tracking status fill/partial/cancelled
"""
import asyncio
import time
import logging
import hmac
import hashlib
import base64
import json
import uuid
from typing import Optional, Tuple, Dict
import aiohttp

from . import config
from .feeds import STATE as feed

log = logging.getLogger("volkovx.executor")

CLOB_HOST = "https://clob.polymarket.com"

# Cache untuk balance (TTL kecil)
_BALANCE_CACHE: Dict[str, float] = {"value": 0.0, "ts": 0.0}
_BALANCE_TTL_S = 5.0

# Status code yang transient (boleh retry)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


# ─────────────────────────────────────────────────────────────
#  Auth signing
# ─────────────────────────────────────────────────────────────
def _sign_request(method: str, path: str, body: str = "") -> dict:
    """Generate Polymarket CLOB API auth headers (HMAC-SHA256, base64)."""
    if not (config.API_SECRET and config.API_KEY and config.API_PASSPHRASE):
        raise RuntimeError("API credentials belum lengkap di config")

    ts  = str(int(time.time() * 1000))
    msg = ts + method.upper() + path + body

    # Padding b64 secret dengan benar (kelipatan 4)
    secret_padded = config.API_SECRET + "=" * (-len(config.API_SECRET) % 4)
    try:
        secret_b = base64.b64decode(secret_padded)
    except Exception as e:
        raise RuntimeError(f"API_SECRET bukan base64 valid: {e}")

    sig     = hmac.new(secret_b, msg.encode(), hashlib.sha256).digest()
    sig_b64 = base64.b64encode(sig).decode()

    return {
        "POLY-API-KEY":    config.API_KEY,
        "POLY-SIGNATURE":  sig_b64,
        "POLY-TIMESTAMP":  ts,
        "POLY-PASSPHRASE": config.API_PASSPHRASE,
        "Content-Type":    "application/json",
    }


# ─────────────────────────────────────────────────────────────
#  Order placement
# ─────────────────────────────────────────────────────────────
async def place_market_order(
    session: aiohttp.ClientSession,
    direction: str,
    stake_usd: float,
    *,
    max_retries: int = 2,
    slippage_tol: float = 0.02,
) -> Tuple[bool, str]:
    """Pasang market order ke Polymarket CLOB.

    Args:
        direction: "UP" atau "DOWN"
        stake_usd: jumlah USDC yang dipertaruhkan
        max_retries: max retry untuk transient error
        slippage_tol: toleransi slippage absolute (0.02 = ±2¢)

    Returns:
        (success: bool, order_id_or_error_msg: str)
    """
    # ── Pre-flight checks ────────────────────────────────────
    if direction not in ("UP", "DOWN"):
        return False, f"Invalid direction: {direction}"
    if stake_usd <= 0:
        return False, f"Invalid stake: {stake_usd}"

    token_id = feed.token_up if direction == "UP" else feed.token_down
    if not token_id:
        return False, f"No token_id for {direction}"

    odds = feed.odds_up if direction == "UP" else feed.odds_down
    if not (0.01 < odds < 0.99):
        return False, f"Odds {odds:.3f} di luar range valid"

    # Limit price dengan slippage tolerance
    limit_price = round(min(0.99, odds + slippage_tol), 4)
    size = round(stake_usd / max(odds, 0.01), 2)
    if size <= 0:
        return False, f"Computed size {size} invalid"

    # ── DRY mode ─────────────────────────────────────────────
    if config.DRY_RUN:
        log.info(
            f"[DRY] BET {direction} ${stake_usd:.2f} | "
            f"odds={odds:.3f} limit={limit_price:.3f} size={size:.2f} | "
            f"token={str(token_id)[:8]}..."
        )
        return True, f"DRY-{uuid.uuid4().hex[:8]}"

    # ── LIVE mode ────────────────────────────────────────────
    body_dict = {
        "tokenID":       token_id,
        "price":         limit_price,
        "side":          "BUY",
        "size":          size,
        "orderType":     "MARKET",
        "timeInForce":   "FOK",
        "clientOrderId": str(uuid.uuid4()),
    }
    body = json.dumps(body_dict, separators=(",", ":"))
    path = "/order"
    url  = f"{CLOB_HOST}{path}"

    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            headers = _sign_request("POST", path, body)
            async with session.post(
                url, data=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                # Try to parse JSON, fallback to text
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError):
                    data = {"raw": (await resp.text())[:200]}

                if resp.status == 200 and isinstance(data, dict) and data.get("success"):
                    order_id = data.get("orderID") or data.get("orderId") or "?"
                    log.info(
                        f"✅ Order placed: {direction} ${stake_usd:.2f} "
                        f"@ {limit_price:.3f} | ID={order_id}"
                    )
                    # Invalidate balance cache
                    _BALANCE_CACHE["ts"] = 0
                    return True, str(order_id)

                err = (data.get("error") if isinstance(data, dict) else str(data)) or f"HTTP {resp.status}"
                last_err = f"{resp.status}: {err}"

                # Retry hanya untuk transient errors
                if resp.status in _TRANSIENT_STATUS and attempt < max_retries:
                    delay = 0.5 * (2 ** attempt)
                    log.warning(f"Order transient err {last_err}, retry in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                log.error(f"❌ Order failed: {last_err}")
                return False, last_err

        except asyncio.TimeoutError:
            last_err = "Timeout"
            log.warning(f"Order timeout (attempt {attempt+1})")
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            return False, "Timeout after retries"
        except aiohttp.ClientError as e:
            last_err = f"Network: {e}"
            log.warning(f"Order network err: {e}")
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            return False, last_err
        except Exception as e:
            log.error(f"❌ Executor exception: {e}")
            return False, str(e)

    return False, last_err or "Unknown failure"


# ─────────────────────────────────────────────────────────────
#  Balance fetch (with cache)
# ─────────────────────────────────────────────────────────────
async def get_balance(session: aiohttp.ClientSession, *, force: bool = False) -> Optional[float]:
    """Ambil saldo USDC. TTL cache 5s untuk hindari rate limit."""
    if config.DRY_RUN:
        return 999.0

    now = time.time()
    if not force and (now - _BALANCE_CACHE["ts"]) < _BALANCE_TTL_S:
        return _BALANCE_CACHE["value"]

    path = "/balance"
    try:
        headers = _sign_request("GET", path)
        async with session.get(
            f"{CLOB_HOST}{path}", headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                log.warning(f"Balance HTTP {resp.status}")
                return None
            data = await resp.json(content_type=None)
            balance = float(data.get("balance", 0))
            _BALANCE_CACHE["value"] = balance
            _BALANCE_CACHE["ts"]    = now
            return balance
    except Exception as e:
        log.warning(f"Balance fetch error: {e}")
        return None


# ─────────────────────────────────────────────────────────────
#  Order status check
# ─────────────────────────────────────────────────────────────
async def get_order_status(session: aiohttp.ClientSession, order_id: str) -> Optional[dict]:
    """Cek status order. Return dict dengan field status/filled."""
    if config.DRY_RUN or order_id.startswith("DRY-"):
        return {"status": "FILLED", "filled_size": "1.0"}

    path = f"/order/{order_id}"
    try:
        headers = _sign_request("GET", path)
        async with session.get(
            f"{CLOB_HOST}{path}", headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as e:
        log.debug(f"Order status err: {e}")
        return None


# ─────────────────────────────────────────────────────────────
#  Auto-claim (gas-free via relayer)
# ─────────────────────────────────────────────────────────────
async def auto_claim(session: aiohttp.ClientSession):
    """Claim semua payout via Relayer API."""
    if not config.AUTO_CLAIM or not config.RELAYER_API_KEY:
        return
    try:
        headers = {
            "Authorization": f"Bearer {config.RELAYER_API_KEY}",
            "Content-Type":  "application/json",
        }
        body = json.dumps({"address": config.FUNDER_ADDRESS})
        async with session.post(
            "https://relayer.polymarket.com/redeem",
            data=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                log.info("AutoClaim: OK")
            else:
                log.debug(f"AutoClaim HTTP {resp.status}")
    except Exception as e:
        log.debug(f"AutoClaim error: {e}")
