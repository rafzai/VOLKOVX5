"""
volkovx/dashboard.py — Rich TUI dashboard real-time (v4)

Improvements:
  - Tampil signal score + breakdown komponen
  - OBI & volatility indicator
  - Color-coded health status (green/yellow/red)
  - Progress bar untuk window timer
  - Sparkline equity curve di footer (mini)
"""
import time
from datetime import datetime
from typing import Optional, List
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.layout import Layout
from rich.progress_bar import ProgressBar
from rich import box

from .feeds import STATE as feed
from . import config

console = Console()


def _color_health(ok: bool) -> str:
    return "[green]●[/green]" if ok else "[red]●[/red]"


def _color_pnl(v: float) -> str:
    if v > 0: return f"[green]+${v:.2f}[/green]"
    if v < 0: return f"[red]-${abs(v):.2f}[/red]"
    return f"${v:.2f}"


def _fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


def _score_bar(score: float, width: int = 20) -> str:
    """ASCII bar untuk score 0..1."""
    filled = int(score * width)
    color  = "green" if score >= 0.7 else "yellow" if score >= 0.5 else "red"
    bar    = "█" * filled + "░" * (width - filled)
    return f"[{color}]{bar}[/{color}] {score:.2f}"


def build_layout(
    stats:           dict,
    window_ts:       Optional[float],
    opening_price:   Optional[float],
    window_elapsed:  float,
    filter_result,
    active_position: Optional[dict],
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=4),
    )
    layout["body"].split_row(
        Layout(name="left",  ratio=1),
        Layout(name="right", ratio=1),
    )

    # ── Header ──────────────────────────────────────────────
    mode_tag = "[yellow]🎮 DRY[/yellow]" if config.DRY_RUN else "[red blink]💰 LIVE[/red blink]"
    now_str  = datetime.now().strftime("%H:%M:%S")
    market_q = (feed.market_question or "—")[:55]
    header = Panel(
        f"[bold cyan]VOLKOVX BOT v4.0[/bold cyan]  {mode_tag}  |  {now_str}  |  "
        f"Stake: [bold]${config.STAKE_USD:.2f}[/bold]  |  Market: [dim]{market_q}[/dim]",
        box=box.DOUBLE_EDGE,
    )
    layout["header"].update(header)

    # ── Left: Price Feeds & Window ──────────────────────────
    price_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    price_table.add_column(style="dim", width=12)
    price_table.add_column()
    btc = feed.btc_price
    price_table.add_row("BTC", f"[bold yellow]${btc:,.2f}[/bold yellow]" if btc else "[red]N/A[/red]")
    price_table.add_row("Age",     f"{feed.price_age:.1f}s" if btc else "—")
    price_table.add_row("Vol 60s", f"${feed.vol_60s:.1f}" if feed.vol_60s else "—")
    price_table.add_row("OBI",     f"{feed.obi:+.2f}")
    price_table.add_row("Spread",  f"{feed.spread_bps:.1f}bps" if feed.spread_bps else "—")
    price_table.add_row("", "")
    price_table.add_row("Odds UP",   f"[green]{feed.odds_up:.3f}[/green]")
    price_table.add_row("Odds DOWN", f"[red]{feed.odds_down:.3f}[/red]")

    # Health indicators
    price_table.add_row("", "")
    price_table.add_row("Poly RTDS", _color_health(feed.poly_rtds_ok))
    price_table.add_row("HL WS",     _color_health(feed.hl_ws_ok))
    price_table.add_row("Chainlink", _color_health(feed.chain_ok))
    price_table.add_row("Gamma",     _color_health(feed.gamma_ok))

    # Window info
    win_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    win_table.add_column(style="dim", width=12)
    win_table.add_column()
    if opening_price:
        beat_dist = (feed.btc_price or 0) - opening_price
        color = "green" if beat_dist >= 0 else "red"
        win_table.add_row("Open(Beat)", f"${opening_price:,.2f}")
        win_table.add_row("Move",       f"[{color}]{beat_dist:+.2f}[/{color}]")
    else:
        win_table.add_row("Open(Beat)", "[dim]—[/dim]")

    remaining = max(0, config.WINDOW_SEC - window_elapsed)
    win_table.add_row("Elapsed",   f"{window_elapsed:.0f}s")
    win_table.add_row("Remaining", f"[bold]{remaining:.0f}s[/bold]")

    # In-zone indicator
    if config.ENTRY_START_SEC <= window_elapsed <= config.ENTRY_END_SEC:
        win_table.add_row("Zone", "[green]✓ IN ZONE[/green]")
    elif window_elapsed < config.ENTRY_START_SEC:
        wait = config.ENTRY_START_SEC - window_elapsed
        win_table.add_row("Zone", f"[yellow]wait {wait:.0f}s[/yellow]")
    else:
        win_table.add_row("Zone", "[dim]passed[/dim]")

    left_panel = Panel(
        Columns([price_table, win_table]),
        title="[bold]📊 Feeds & Window[/bold]",
        box=box.ROUNDED,
    )
    layout["left"].update(left_panel)

    # ── Right: Filters & Liquidations ───────────────────────
    filt_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    filt_table.add_column(style="bold cyan", width=4)
    filt_table.add_column()
    if filter_result:
        if filter_result.f1: filt_table.add_row("F1", filter_result.f1)
        if filter_result.f2: filt_table.add_row("F2", filter_result.f2)
        if filter_result.f3: filt_table.add_row("F3", filter_result.f3)
        if filter_result.f4: filt_table.add_row("F4", filter_result.f4)
        if filter_result.f5: filt_table.add_row("F5", filter_result.f5)
        if filter_result.f6: filt_table.add_row("F6", filter_result.f6)
        if filter_result.f7: filt_table.add_row("F7", filter_result.f7)
        if filter_result.f8: filt_table.add_row("F8", filter_result.f8)

        # Score bar
        if filter_result.score > 0:
            filt_table.add_row("", "")
            filt_table.add_row("[bold]Score[/bold]", _score_bar(filter_result.score))

    liq_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    liq_table.add_column(style="dim", width=14)
    liq_table.add_column()
    liq_table.add_row("Liq SHORT 3s",  _fmt_usd(feed.liq_short_3s))
    liq_table.add_row("Liq SHORT 30s", _fmt_usd(feed.liq_short_30s))
    liq_table.add_row("Liq LONG 3s",   _fmt_usd(feed.liq_long_3s))
    liq_table.add_row("Liq LONG 30s",  _fmt_usd(feed.liq_long_30s))
    liq_table.add_row("CVD 30s",       f"{feed.cvd_30s:+,.0f}")
    liq_table.add_row("CVD 2min",      f"{feed.cvd_2min:+,.0f}")

    right_panel = Panel(
        Columns([filt_table, liq_table]),
        title="[bold]🎯 Filters & Liquidations[/bold]",
        box=box.ROUNDED,
    )
    layout["right"].update(right_panel)

    # ── Footer: Stats + Position ─────────────────────────────
    wins     = stats.get("wins", 0)
    losses   = stats.get("losses", 0)
    total    = wins + losses
    win_rate = wins / total * 100 if total else 0
    pnl      = stats.get("pnl", 0.0)
    balance  = stats.get("balance", 0.0)
    blocked  = stats.get("blocked", 0)
    avg_pnl  = stats.get("avg_pnl", 0.0)

    pos_str = ""
    if active_position:
        d   = active_position.get("direction", "?")
        col = "green" if d == "UP" else "red"
        pos_str = f"  |  ⚡ Active: [{col}]{d}[/{col}] ${active_position.get('stake', 0):.2f}"

    footer_text = (
        f"💰 Balance: [bold]${balance:.2f}[/bold]  "
        f"|  W: [green]{wins}[/green]  L: [red]{losses}[/red]  "
        f"WR: [bold]{win_rate:.1f}%[/bold]  "
        f"|  PnL: {_color_pnl(pnl)}  "
        f"|  Avg: {_color_pnl(avg_pnl)}  "
        f"|  Blocked: {blocked}{pos_str}"
    )
    layout["footer"].update(Panel(footer_text, box=box.SIMPLE))

    return layout
