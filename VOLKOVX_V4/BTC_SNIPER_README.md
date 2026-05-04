# VOLKOVX BOT v4.0

### Polymarket BTC UP/DOWN 5-min Late-Entry Sniper — Advanced Edition

Bot otomatis untuk trading di Polymarket pada market **Bitcoin Up or Down (5 menit)**.
Versi 4 menambahkan signal scoring, dynamic position sizing, dan risk circuit breakers.

---

## Cara Jalankan

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Setup .env
cp .env.example .env
# (Windows: copy .env.example .env)
# Edit .env, isi private key & API credentials

# 3. Generate API key Polymarket
python generate_api_creds.py

# 4. Jalankan bot (DRY mode default)
python -m volkovx

# 5. Analisis hasil
python analyze.py

# 6. Run unit tests
python tests/test_core.py
```

---

## Apa yang baru di v4 (vs v3)

### 🐛 Bug Fixes (kritis)

| # | Bug v3 | Fix v4 |
|---|--------|--------|
| 1 | **Duplicate bet recording** — `stats.bets.append(bet)` dipanggil dari main_loop, lalu `record_bet` (yang juga append) → bet tercatat 2x di list dan CSV | Hanya `record_bet()` yang menambah ke list & CSV. Main loop hanya memanggil `record_bet()` sekali |
| 2 | **No bet resolution** — v3 tidak punya logic untuk resolve bet; PnL tetap 0, win/loss tidak update, daily SL tidak pernah trigger | `resolve_pending_bets()` dipanggil di setiap window baru, hitung PnL berdasarkan harga close vs beat |
| 3 | **Opening price null trap** — kalau saat window berganti harga belum siap, `opening_price = None`, window itu tidak akan bisa fire bet selamanya | Deferred capture: terus mencoba capture sampai harga tersedia |
| 4 | **CPU spin / timing race** — `if int(now) % 60 == 0` bisa miss atau double-trigger | Timestamp counter `last_balance_check_ts` |
| 5 | **Blackout continue bug** — saat blackout, `continue` membuat window detection di iterasi berikutnya rusak | Tetap proses window detection, hanya skip filter+bet |
| 6 | **No graceful shutdown** — KeyboardInterrupt drop background tasks tanpa cleanup | Cancel feeds, flush logs, await pending tasks |
| 7 | **List-based rolling buffer** — `[(t,s,z) for t,s,z in liq_window if t > now - 30]` di-rebuild setiap event → O(n²) seiring waktu | `collections.deque(maxlen=...)` dengan O(1) prune dari kiri |
| 8 | **Aggregator stale** — kalau tidak ada event masuk, nilai `liq_short_30s` stuck di angka lama bukannya drop ke 0 | `feed_aggregator_tick()` recompute setiap 1s berdasarkan timestamps |
| 9 | **Reconnect hammer** — `await asyncio.sleep(5)` setelah disconnect → kalau server down bot bombarding | Exponential backoff dengan jitter |
| 10 | **Price staleness blind** — `feed.btc_price` return value lama walaupun feed mati | Property dengan max-age check per source |
| 11 | **Padding base64 risk** — `secret + "=="` bisa salah bila length sudah bagus | `secret + "=" * (-len % 4)` |
| 12 | **No idempotency** — retry order bisa double-submit | UUID `clientOrderId` |
| 13 | **No retries** — single failure → no bet | Retry transient errors (5xx, timeout) dengan backoff |
| 14 | **Unbounded TCP connections** — bisa habis file descriptor | TCPConnector dengan limit |

### 🚀 Performance Improvements

- **deque dengan maxlen** untuk rolling window → O(1) operasi, ganti list slicing yang O(n) per event
- **Pre-aggregated counters** untuk liquidations & CVD → query sangat cepat
- **Aggregator tick** terpisah supaya nilai stale benar-benar drop ke 0 (bukan stuck di nilai lama)
- **Balance cache** dengan TTL 5s → kurangi rate limit risk
- **Atomic snapshot** dari feed state → konsistensi multi-field reads

### 🎯 Filter Improvements

- **F5 Odds Sanity** — skip bet kalau odds di luar 0.20-0.80 (RR jelek)
- **F7 Price Staleness** — skip kalau harga > 5s tanpa update
- **F8 Composite Signal Score** — weighted sum dari 6 komponen, threshold ≥ 0.55
- **CVD short-term confirmation** — CVD 30s harus searah dengan CVD 2min
- **OBI bonus** — order book imbalance jadi sinyal tambahan, bukan veto

### 💰 Risk Management (NEW)

- **Dynamic position sizing** (Kelly-lite) — stake mengikuti signal score & balance
- **Consecutive loss tracking** — N losses berturut → cooldown panjang
- **Drawdown circuit breaker** — halt kalau DD > 50% peak
- **Daily reset** — stats di-reset di rollover hari (UTC)

### 📊 Better Observability

- Dashboard menampilkan signal score bar + breakdown komponen
- OBI, volatility, spread di tampilkan
- Health indicator per feed (RTDS / HL / Chainlink / Gamma)
- Equity curve CSV untuk plotting offline
- `analyze.py` upgrade: per-direction, per-score-bin, Sharpe, max streaks, blocked breakdown

---

## Struktur Folder

```
VOLKOVX/
├── volkovx/                  ← module utama bot
│   ├── __init__.py
│   ├── __main__.py           ← entry point (python -m volkovx)
│   ├── config.py             ← config loader & validator
│   ├── feeds.py              ← real-time data feeds (WS + REST)
│   ├── filters.py            ← multi-filter pipeline + signal score
│   ├── risk.py               ← risk management (NEW di v4)
│   ├── executor.py           ← order placement
│   ├── session.py            ← stats & CSV logging
│   └── dashboard.py          ← TUI dashboard
├── tests/
│   └── test_core.py          ← unit tests (30 cases)
├── data/                     ← runtime data
├── logs/                     ← log & CSV hasil bet
├── .env.example              ← template config
├── .gitignore
├── README.md
├── analyze.py                ← analisis hasil trading
├── generate_api_creds.py     ← generate API creds dari private key
└── requirements.txt
```

---

## Filter Pipeline (v4)

| Filter | Tugas | Default |
|--------|-------|---------|
| **F1** | Entry zone timing | t=210s..295s |
| **F2** | Beat distance (price moved enough) | ≥ $40 |
| **F3** | Liquidation cascade dual-window | 3s≥$15K, 30s≥$50K |
| **F4** | CVD alignment (delta order flow) | \|CVD\|≥$25K, searah |
| **F5** | Odds sanity (RR sehat) | 0.20–0.80 |
| **F6** | Gocek cooldown (after small loss) | 90 min |
| **F7** | Price freshness | ≤ 5s age |
| **F8** | Composite signal score | ≥ 0.55 |

Semua filter harus PASS, dan semua arah (F2/F3/F4/OBI) harus searah.

---

## Risk Management

```
Stake = base × (0.5 + score) × max(0.5, 1 − 0.30 × consec_losses)
       └─ score multiplier ─┘   └─ loss dampener ─┘
```

Lalu di-clamp: `max(MIN_STAKE_USD, min(stake, balance × MAX_STAKE_PCT))`

**Circuit breakers:**
- Daily SL hit → halt
- Max DD ≥ 50% dari peak → halt
- N losses berturut → long cooldown (default 60 min)

---

## Output Files

| File | Isi |
|------|-----|
| `logs/volkovx_results.csv` | Tiap bet yang ditempatkan & resolved |
| `logs/volkovx_blocked.csv` | Tiap signal yang di-block oleh filter |
| `logs/volkovx_equity.csv` | Snapshot balance & PnL kumulatif |
| `logs/volkovx_live.log` | Log runtime detail |

---

## Disclaimer

Trading prediksi pasar mengandung risiko kerugian total. Software ini disediakan AS-IS, tanpa garansi apapun. Selalu mulai dengan **DRY mode** untuk verifikasi behavior, kemudian dengan stake kecil sebelum naik. Operator yang menjalankan bot bertanggung jawab atas semua keputusan & dana yang digunakan.
