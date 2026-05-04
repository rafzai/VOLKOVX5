"""
volkovx/config.py — Load & validate config dari .env

v4 improvements:
  - Validasi range untuk semua nilai numerik (mencegah config invalid)
  - Tipe konsisten + helper _getint
  - Konstanta dipisah jadi grup yang jelas
  - Validasi entry zone (start < end <= window)
  - Fail-fast saat live mode tanpa kredensial
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

log = logging.getLogger("volkovx.config")

# Load .env dari root folder VOLKOVX
_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def _get(key: str, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise EnvironmentError(f"[CONFIG] Field wajib '{key}' tidak ada di .env")
    return val


def _getfloat(key, default=0.0, min_val=None, max_val=None):
    try:
        v = float(_get(key, str(default)))
    except (TypeError, ValueError):
        log.warning(f"Config {key} invalid, pakai default {default}")
        v = default
    if min_val is not None and v < min_val:
        log.warning(f"Config {key}={v} di bawah min {min_val}, clamp")
        v = min_val
    if max_val is not None and v > max_val:
        log.warning(f"Config {key}={v} di atas max {max_val}, clamp")
        v = max_val
    return v


def _getint(key, default=0, min_val=None, max_val=None):
    try:
        v = int(_get(key, str(default)))
    except (TypeError, ValueError):
        log.warning(f"Config {key} invalid, pakai default {default}")
        v = default
    if min_val is not None and v < min_val:
        v = min_val
    if max_val is not None and v > max_val:
        v = max_val
    return v


def _getbool(key, default=True):
    val = _get(key, str(default)).strip().lower()
    return val in ("true", "1", "yes", "on", "y")


# ─────────────────────────────────────────────────────────────
#  Polymarket credentials
# ─────────────────────────────────────────────────────────────
PRIVATE_KEY     = _get("POLYMARKET_PRIVATE_KEY")
FUNDER_ADDRESS  = _get("POLYMARKET_FUNDER")
API_KEY         = _get("POLYMARKET_API_KEY")
API_SECRET      = _get("POLYMARKET_API_SECRET")
API_PASSPHRASE  = _get("POLYMARKET_API_PASSPHRASE")

# Relayer (auto-claim)
RELAYER_API_KEY         = _get("RELAYER_API_KEY", "")
RELAYER_API_KEY_ADDRESS = _get("RELAYER_API_KEY_ADDRESS", "")


# ─────────────────────────────────────────────────────────────
#  Trading params
# ─────────────────────────────────────────────────────────────
DRY_RUN      = _getbool("VOLKOVX_DRY_RUN", True)
STAKE_USD    = _getfloat("VOLKOVX_STAKE_USD", 2.0, min_val=0.5, max_val=10_000)
DAILY_SL_PCT = _getfloat("VOLKOVX_DAILY_SL_PCT", 0.50, min_val=0.05, max_val=1.0)
AUTO_CLAIM   = _getbool("VOLKOVX_AUTO_CLAIM", True)

# v4: position sizing dinamis (Kelly-lite)
USE_DYNAMIC_SIZING = _getbool("VOLKOVX_DYNAMIC_SIZING", False)
MAX_STAKE_PCT      = _getfloat("VOLKOVX_MAX_STAKE_PCT", 0.05, min_val=0.005, max_val=0.25)
MIN_STAKE_USD      = _getfloat("VOLKOVX_MIN_STAKE_USD", 1.0, min_val=0.5)

# v4: max consecutive losses sebelum cooldown panjang
MAX_CONSEC_LOSSES = _getint("VOLKOVX_MAX_CONSEC_LOSSES", 3, min_val=1, max_val=20)
LONG_COOLDOWN_MIN = _getint("VOLKOVX_LONG_COOLDOWN_MIN", 60, min_val=5, max_val=720)


# ─────────────────────────────────────────────────────────────
#  Filter thresholds
# ─────────────────────────────────────────────────────────────
F2_BEAT_DIST_MIN = _getfloat("F2_BEAT_DIST_MIN", 40.0, min_val=1.0)
F3_LIQ_3S_MIN    = _getfloat("F3_LIQ_3S_MIN", 15_000, min_val=0)
F3_LIQ_30S_MIN   = _getfloat("F3_LIQ_30S_MIN", 50_000, min_val=0)
F4_CVD_MIN       = _getfloat("F4_CVD_MIN", 25_000, min_val=0)

# v4: filter tambahan
F5_ODDS_MIN          = _getfloat("F5_ODDS_MIN", 0.20, min_val=0.05, max_val=0.95)
F5_ODDS_MAX          = _getfloat("F5_ODDS_MAX", 0.80, min_val=0.05, max_val=0.95)
F7_PRICE_STALENESS_S = _getfloat("F7_PRICE_STALENESS_S", 5.0, min_val=1.0)
F8_MIN_SIGNAL_SCORE  = _getfloat("F8_MIN_SIGNAL_SCORE", 0.55, min_val=0.0, max_val=1.0)


# ─────────────────────────────────────────────────────────────
#  Timing
# ─────────────────────────────────────────────────────────────
WINDOW_SEC      = 300
ENTRY_START_SEC = _getint("ENTRY_START_SEC", 210, min_val=0, max_val=WINDOW_SEC - 1)
ENTRY_END_SEC   = _getint("ENTRY_END_SEC",   295, min_val=0, max_val=WINDOW_SEC - 1)

# Validasi entry zone
if ENTRY_END_SEC <= ENTRY_START_SEC:
    log.warning(f"ENTRY_END_SEC ({ENTRY_END_SEC}) <= ENTRY_START_SEC ({ENTRY_START_SEC}), reset ke default")
    ENTRY_START_SEC, ENTRY_END_SEC = 210, 295


# ─────────────────────────────────────────────────────────────
#  Session blackouts (UTC hour)
# ─────────────────────────────────────────────────────────────
def _parse_hours(s: str, default: list) -> list:
    if not s:
        return default
    try:
        return [int(x.strip()) for x in s.split(",") if x.strip().isdigit()]
    except Exception:
        return default

BLACKOUT_HOURS_UTC = _parse_hours(
    _get("BLACKOUT_HOURS_UTC", "8,13,20"),
    default=[8, 13, 20],
)


# ─────────────────────────────────────────────────────────────
#  Misc
# ─────────────────────────────────────────────────────────────
GOCEK_COOLDOWN_MIN = _getint("GOCEK_COOLDOWN_MIN", 90, min_val=5, max_val=720)

LOG_DIR  = Path(__file__).parent.parent / "logs"
DATA_DIR = Path(__file__).parent.parent / "data"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# v4: log level configurable
LOG_LEVEL = _get("VOLKOVX_LOG_LEVEL", "INFO").upper()


# ─────────────────────────────────────────────────────────────
#  Validation
# ─────────────────────────────────────────────────────────────
def validate_for_live() -> list:
    """Return list of error messages jika config tidak siap untuk LIVE mode."""
    errors = []
    if not PRIVATE_KEY or PRIVATE_KEY.startswith("0x_PASTE"):
        errors.append("POLYMARKET_PRIVATE_KEY belum diisi di .env")
    if not FUNDER_ADDRESS or FUNDER_ADDRESS.startswith("0x_PASTE"):
        errors.append("POLYMARKET_FUNDER belum diisi di .env")
    if not API_KEY:
        errors.append("POLYMARKET_API_KEY kosong (jalankan: python generate_api_creds.py)")
    if not API_SECRET:
        errors.append("POLYMARKET_API_SECRET kosong")
    if not API_PASSPHRASE:
        errors.append("POLYMARKET_API_PASSPHRASE kosong")
    return errors
