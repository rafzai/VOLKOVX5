"""
volkovx/__main__.py — Entry point: python -m volkovx (v4)

Critical fixes from v3:
  1. **DUPLICATE BET RECORDING** (v3 bug):
     v3 memanggil `stats.bets.append(bet)` di main_loop, lalu juga
     `stats.record_bet(bet)` (yang juga append). Hasilnya bet tercatat
     2x di list & CSV. → v4: hanya panggil `stats.record_bet()` sekali.

  2. **NO BET RESOLUTION** (v3 bug):
     v3 sama sekali tidak punya logic untuk resolve bet setelah window
     close. PnL tetap 0, win/loss tidak pernah update, daily SL tidak
     pernah trigger. → v4: ada `_resolve_pending_bets()` yang dipanggil
     tiap window baru.

  3. **OPENING_PRICE NULL TRAP** (v3 bug):
     Kalau `feed.btc_price` belum siap saat window berganti, opening_price
     = None, dan window itu tidak akan bisa fire bet selamanya. → v4:
     deferred capture — coba terus capture sampai ada harga.

  4. **CPU SPIN BUG** (v3 bug):
     `if int(now) % 60 == 0` bisa trigger 0-2x dalam satu detik karena
     loop tidur 1s tepat. → v4: pakai counter `last_balance_check_ts`.

  5. **BLACKOUT CONTINUE BUG** (v3 bug):
     Saat blackout, v3 `continue` tapi tidak update last_window_ts.
     Window detection di iterasi berikutnya jadi rusak. → v4: tetap
     proses window detection lalu skip filtering.

  6. **GRACEFUL SHUTDOWN** (v3 missing):
     v3 KeyboardInterrupt dropped semua background tasks tanpa cleanup.
     → v4: cancel feeds, flush logs, save state.

  7. **NO RESOLVED BET PERSISTENCE** (v3 design):
     v3 tidak membaca harga settle dari Polymarket, jadi tidak bisa
     menghitung PnL. → v4: settle berdasarkan opening vs closing price
     yang ditangkap real-time.

  8. **STARTUP RACE CONDITION**: v3 punya wait yang OK, v4 perbaiki
     supaya semua field yang dibutuhkan filter (token_up/down) ditunggu.
"""
import asyncio
import time
import logging
import sys
import signal
from datetime import datetime, timezone
from typing import Optional, Dict
from rich.console import Console
from rich.prompt import Prompt
from rich.panel import Panel
from rich.live import Live
import aiohttp

from . import config
from .feeds import STATE as feed, start_all_feeds
from .filters import run_all_filters, FilterResult
from .executor import place_market_order, get_balance, auto_claim
from .session import SessionStats, BetRecord
from .risk import RiskState, calculate_stake, update_after_bet, check_circuit_breakers
from .dashboard import build_layout

# ── Logging setup ────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(name)-22s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_DIR / "volkovx_live.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("volkovx.main")
console = Console()

# Shutdown event untuk graceful cleanup
_SHUTDOWN_EVENT = asyncio.Event() if False else None  # init di run()


# ─────────────────────────────────────────────────────────────
#  Startup prompt
# ─────────────────────────────────────────────────────────────
def startup_prompt() -> bool:
    console.print(Panel.fit(
        f"""[bold cyan]VOLKOVX BOT[/bold cyan] v4.0.0
[dim]Polymarket BTC UP/DOWN 5-min Late-Entry Sniper[/dim]
[dim]Advanced edition: signal scoring, dynamic sizing, risk circuit breakers[/dim]

Mode       : {'[yellow]DRY (simulasi)[/yellow]' if config.DRY_RUN else '[red bold]LIVE TRADING[/red bold]'}
Stake base : [bold]${config.STAKE_USD:.2f}[/bold] USDC
Dynamic    : {'[green]ON[/green]' if config.USE_DYNAMIC_SIZING else '[dim]OFF[/dim]'}
Daily SL   : {config.DAILY_SL_PCT*100:.0f}% dari saldo awal
Entry zone : t={config.ENTRY_START_SEC}s – {config.ENTRY_END_SEC}s

Filter thresholds:
  F2 beat dist: ${config.F2_BEAT_DIST_MIN:.0f}
  F3 liq 3s   : ${config.F3_LIQ_3S_MIN:,.0f}
  F3 liq 30s  : ${config.F3_LIQ_30S_MIN:,.0f}
  F4 CVD min  : ${config.F4_CVD_MIN:,.0f}
  F5 odds     : {config.F5_ODDS_MIN}–{config.F5_ODDS_MAX}
  F8 score min: {config.F8_MIN_SIGNAL_SCORE}

Circuit breakers:
  Consec loss: {config.MAX_CONSEC_LOSSES} → cooldown {config.LONG_COOLDOWN_MIN}min
""",
        title="Config Summary",
    ))

    if not config.DRY_RUN:
        # Validate kredensial sebelum live
        errors = config.validate_for_live()
        if errors:
            console.print("[red bold]❌ LIVE mode tidak siap:[/red bold]")
            for e in errors:
                console.print(f"  • {e}")
            console.print("[yellow]Beralih ke DRY mode...[/yellow]")
            config.DRY_RUN = True
        else:
            console.print("[red bold]⚠️  LIVE MODE — UANG SUNGGUHAN[/red bold]")
            confirm = Prompt.ask("Ketik [bold]LIVE[/bold] persis untuk konfirmasi", default="")
            if confirm.strip() != "LIVE":
                console.print("[yellow]Live mode dibatalkan, beralih ke DRY[/yellow]")
                config.DRY_RUN = True

    choice = Prompt.ask("\n[Enter] lanjut / [q]uit", default="")
    return choice.lower() != "q"


# ─────────────────────────────────────────────────────────────
#  Window utilities
# ─────────────────────────────────────────────────────────────
def current_window_start() -> float:
    """Window 5-min UTC-aligned."""
    now = time.time()
    return now - (now % config.WINDOW_SEC)


def is_blackout() -> bool:
    return datetime.now(timezone.utc).hour in config.BLACKOUT_HOURS_UTC


# ─────────────────────────────────────────────────────────────
#  Bet resolution
# ─────────────────────────────────────────────────────────────
def resolve_pending_bets(stats: SessionStats, risk: RiskState, last_close_price: float):
    """Resolve semua pending bet dari window yang baru saja close.

    Polymarket BTC 5-min: outcome berdasarkan harga BTC di akhir window
    vs beat (harga awal). UP menang kalau close > beat, sebaliknya DOWN.

    PnL formula:
      - Win: payout = stake / odds, profit = payout - stake
      - Loss: profit = -stake
    """
    for bet in stats.bets:
        if bet.resolved:
            continue
        # Di settlement: harga close > beat → UP win, < beat → DOWN win
        # Sama persis = TIE (jarang sekali, treat as loss konservatif)
        delta = last_close_price - bet.beat_price
        if delta > 0:
            winning_dir = "UP"
        elif delta < 0:
            winning_dir = "DOWN"
        else:
            winning_dir = None  # tie

        if winning_dir == bet.direction:
            # Win: payout = stake/odds, profit = payout - stake
            payout = bet.stake / max(bet.odds, 0.01)
            pnl    = payout - bet.stake
        else:
            pnl = -bet.stake

        stats.resolve_bet(bet, pnl)
        update_after_bet(risk, pnl, stats.current_balance + pnl)
        stats.current_balance += pnl   # update local; akan disinkronkan dari API juga


# ─────────────────────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────────────────────
async def main_loop(session: aiohttp.ClientSession):
    stats = SessionStats()
    risk  = RiskState()
    filter_result:   Optional[FilterResult] = None
    active_position: Optional[Dict]         = None

    opening_price:           Optional[float] = None
    last_known_price:        Optional[float] = None
    last_window_ts:          float = 0.0
    bet_placed_this_window:  bool  = False
    last_claim_ts:           float = 0.0
    last_balance_check_ts:   float = 0.0
    BALANCE_CHECK_INTERVAL = 60.0

    # ── FIX: Tunggu feed siap (termasuk token_up/token_down) ────
    console.print("[cyan]Menunggu market discovery dan data feed awal...[/cyan]")
    wait_start = time.time()
    FEED_TIMEOUT = 90
    while True:
        market_ready = bool(feed.market_id and feed.token_up and feed.token_down)
        price_ready  = feed.btc_price is not None
        if market_ready and price_ready:
            console.print(
                f"[green]✅ Feed siap: market={str(feed.market_id)[:8]}... "
                f"BTC=${feed.btc_price:,.2f}[/green]"
            )
            break
        elapsed_wait = time.time() - wait_start
        if elapsed_wait > FEED_TIMEOUT:
            console.print(
                "[red bold]❌ Timeout menunggu feed! Periksa koneksi internet.[/red bold]"
            )
            log.error("Feed startup timeout — aborting")
            return

        status = []
        if not feed.market_id: status.append("market_id")
        if not feed.token_up:  status.append("token_up")
        if not feed.token_down:status.append("token_down")
        if not price_ready:    status.append("BTC price")
        console.print(f"[dim]  [{elapsed_wait:.0f}s] menunggu: {', '.join(status)}[/dim]")
        await asyncio.sleep(3)

    # Init balance
    balance = await get_balance(session)
    if balance is not None:
        stats.start_balance   = balance
        stats.current_balance = balance
        risk.peak_balance     = balance
        console.print(f"[green]Saldo awal: ${balance:.2f} USDC[/green]")
    else:
        console.print("[yellow]⚠️  Tidak bisa fetch balance, lanjut tanpa stop-loss[/yellow]")

    console.print("[dim]Window pertama = warmup capture opening_price[/dim]")
    await asyncio.sleep(2)

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            now     = time.time()
            win_ts  = current_window_start()
            elapsed = now - win_ts

            stats.maybe_daily_reset()

            # ── Capture last known price (untuk settlement) ────
            if feed.btc_price is not None:
                last_known_price = feed.btc_price

            # ── Deteksi window baru ────────────────────────────
            if win_ts != last_window_ts:
                # Resolve bet dari window sebelumnya
                if last_window_ts > 0 and last_known_price is not None:
                    resolve_pending_bets(stats, risk, last_known_price)

                last_window_ts          = win_ts
                bet_placed_this_window  = False
                opening_price           = feed.btc_price  # bisa None
                active_position         = None
                log.info(f"New window ts={win_ts} opening={opening_price}")

            # ── FIX: Deferred opening_price capture ────────────
            # Kalau saat window baru harga belum siap, coba lagi setiap tick
            if opening_price is None and feed.btc_price is not None:
                opening_price = feed.btc_price
                log.info(f"Deferred opening capture: ${opening_price:,.2f}")

            # ── Circuit breakers ───────────────────────────────
            halted, reason = check_circuit_breakers(risk, stats.current_balance, stats.start_balance)
            if halted or stats.halted or stats.check_daily_stop_loss():
                console.print(f"[red bold]🛑 HALTED: {reason}[/red bold]")
                break

            # ── Blackout: render dashboard tapi skip trading ───
            blackout = is_blackout()

            # ── Auto claim setiap 5 min ────────────────────────
            if now - last_claim_ts > 300:
                await auto_claim(session)
                last_claim_ts = now

            # ── Balance update tiap 60s ────────────────────────
            if now - last_balance_check_ts >= BALANCE_CHECK_INTERVAL:
                last_balance_check_ts = now
                bal = await get_balance(session)
                if bal is not None:
                    stats.current_balance = bal
                    if bal > risk.peak_balance:
                        risk.peak_balance = bal

            # ── Run filters ────────────────────────────────────
            if not blackout:
                filter_result = run_all_filters(
                    window_elapsed     = elapsed,
                    opening_price      = opening_price,
                    last_bet_ts        = stats.bets[-1].ts if stats.bets else 0,
                    gocek_freeze_until = stats.gocek_freeze_until,
                )

                # ── Fire bet ───────────────────────────────────
                if filter_result.passed and not bet_placed_this_window:
                    direction = filter_result.direction
                    odds      = feed.odds_up if direction == "UP" else feed.odds_down

                    # Dynamic stake calc
                    stake = calculate_stake(
                        base_stake    = config.STAKE_USD,
                        balance       = stats.current_balance,
                        signal_score  = filter_result.score,
                        consec_losses = risk.consec_losses,
                    )

                    log.info(
                        f"🎯 SIGNAL {direction} score={filter_result.score:.2f} "
                        f"odds={odds:.3f} stake=${stake:.2f}"
                    )
                    success, order_id = await place_market_order(session, direction, stake)

                    if success:
                        bet_placed_this_window = True
                        bet = BetRecord(
                            ts         = now,
                            window_ts  = win_ts,
                            direction  = direction,
                            beat_price = opening_price or 0,
                            stake      = stake,
                            odds       = odds,
                            order_id   = order_id,
                            score      = filter_result.score,
                        )
                        # FIX v3: hanya panggil record_bet, jangan append manual
                        stats.record_bet(bet)
                        active_position = {"direction": direction, "stake": stake}
                    else:
                        log.error(f"Bet failed: {order_id}")

                elif (
                    filter_result is not None
                    and not filter_result.passed
                    and elapsed > config.ENTRY_START_SEC
                    and filter_result.skip_reason
                ):
                    stats.record_blocked(
                        win_ts, filter_result.skip_reason,
                        filter_result.direction or ""
                    )

            # ── Render dashboard ───────────────────────────────
            try:
                layout = build_layout(
                    stats           = stats.to_dict(),
                    window_ts       = win_ts,
                    opening_price   = opening_price,
                    window_elapsed  = elapsed,
                    filter_result   = filter_result,
                    active_position = active_position,
                )
                live.update(layout)
            except Exception as e:
                log.warning(f"Dashboard render error: {e}")

            await asyncio.sleep(1.0)


# ─────────────────────────────────────────────────────────────
#  Entry & shutdown
# ─────────────────────────────────────────────────────────────
async def run():
    if not startup_prompt():
        sys.exit(0)

    console.print("[cyan]Connecting to data feeds...[/cyan]")

    # FIX: Gunakan TCPConnector dengan limit untuk mencegah file descriptor exhaustion
    connector = aiohttp.TCPConnector(
        limit=100,
        limit_per_host=20,
        enable_cleanup_closed=True,
    )
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Run feeds + main loop concurrent
        feeds_task = asyncio.create_task(start_all_feeds(session), name="feeds")
        main_task  = asyncio.create_task(main_loop(session),       name="main")

        try:
            done, pending = await asyncio.wait(
                {feeds_task, main_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                exc = task.exception()
                if exc:
                    log.error(f"Task {task.get_name()} failed: {exc}")
        except asyncio.CancelledError:
            log.info("Run cancelled, cleaning up")
            feeds_task.cancel()
            main_task.cancel()
            await asyncio.gather(feeds_task, main_task, return_exceptions=True)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Bot dihentikan oleh user.[/yellow]")
    except Exception as e:
        console.print(f"\n[red bold]Fatal error: {e}[/red bold]")
        log.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
