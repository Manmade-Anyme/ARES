import asyncio
from datetime import datetime, time

from engine import AresEngine
from fetchers.price_fetcher import PriceFetcher
from fetchers.oi_fetcher import OIFetcher
from fetchers.level_fetcher import LevelFetcher
from fetchers.tick_feed import TickFeed
from storage import Storage, load_dhan_credentials_from_supabase
from position_manager import PositionManager
from config import settings
from config_profiles import EXPIRY_CONFIG, NON_EXPIRY_CONFIG
from detectors.expiry_detector import is_expiry_day_from_api, is_expiry_day_simple
from alerts import send_discord, send_startup_alert, send_error_alert
from ml_signal.collector import MLCollector
from options_math import process_options_calculation

# ANSI Color Codes for Premium Terminal UI
G = "\033[92m"  # Green
Y = "\033[93m"  # Yellow
R = "\033[91m"  # Red
C = "\033[96m"  # Cyan
B = "\033[1m"   # Bold
W = "\033[97m"  # White
RESET = "\033[0m"

def print_banner(pdh: float, pdl: float, profile_name: str = "DEFAULT", ml_active: bool = False):
    """Prints the ARES startup banner with configuration details."""
    print(f"{C}{'=' * 65}{RESET}")
    print(f"{C}{B}  ARES (Adaptive Reversal & Entry Signal) - Initialization{RESET}")
    print(f"{C}{'=' * 65}{RESET}")
    profile_color = Y if profile_name == "EXPIRY" else G
    print(f"{profile_color}[+] Config       : {W}{B}{profile_name} DAY PROFILE{RESET}")
    print(f"{G}[+] Target Asset : {W}{settings.yahoo_symbol} (1-minute timeframe){RESET}")
    print(f"{G}[+] Detectors    : {W}Failed Breakout, OI Wall, Exhaustion{RESET}")
    print(f"{G}[+] Session      : {W}09:15 to 15:30 IST{RESET}")
    print(f"{G}[+] Cooldown     : {W}{settings.signal_cooldown_minutes} minutes between signals{RESET}")
    print(f"{G}[+] PDH / PDL    : {W}{pdh:.2f} / {pdl:.2f}{RESET}")
    ml_status = f"{G}ACTIVE (recording 50+ features per cycle)" if ml_active else f"{Y}inactive"
    print(f"{G}[+] ML Data Collection : {W}{ml_status}{RESET}")
    print(f"{Y}[!] Kronos ML Engine   : {W}DISABLED (needs ~3.6GB, VM is 768mb — TASK-190){RESET}")
    print(f"{C}{'=' * 65}{RESET}")

def format_signal_console(signal, spot):
    """Formats and prints a detailed signal alert to the console."""
    color = G if signal.direction.value == "BULLISH" else R
    emoji = "🐂 🟢" if signal.direction.value == "BULLISH" else "🐻 🔴"
    print("\n" + f"{color}{B}━" * 65 + RESET)
    print(f"{color}{B}🚨 {emoji} #{getattr(signal, 'signal_id', '0000')} SIGNAL DETECTED: {signal.setup_type.value} ({signal.direction.value}){RESET}")
    print(f"   {W}Spot  : {spot:.2f}{RESET}")
    print(f"   {W}Trade : {B}{signal.strike_to_trade} {signal.option_type}{RESET}")
    print(f"   {W}Entry : {G}{signal.entry_zone[0]:.2f} - {signal.entry_zone[1]:.2f}{RESET}")
    print(f"   {W}SL    : {R}{signal.stop_loss:.2f} (Spot Ref){RESET}")
    print(f"   {W}Targets: T1={G}{signal.target_1:.2f}{W} | T2={G}{signal.target_2:.2f}{RESET}")
    print(f"   {W}Confidence: {B}{signal.confidence}{RESET}")
    if getattr(signal, "suggested_lots", None) is not None:
        print(f"   {W}Suggested Lots : {G}{signal.suggested_lots}{RESET} ({W}Capital: ₹{signal.capital:,.2f}{RESET} | {W}Risk: {signal.risk_pct:.1f}%{RESET})")
        print(f"   {W}Option SL      : {R}₹{signal.option_sl:.2f}{RESET} | {W}Option Target: {G}₹{signal.option_target:.2f}{RESET} ({W}Premium: ₹{signal.option_premium:.2f}{RESET} | {W}Delta: {signal.option_delta:+.4f}{RESET})")
    print(f"   {W}Reasons:{RESET}")
    for r in signal.reasons:
        print(f"     {W}• {r}{RESET}")
    print(f"{color}{B}━" * 65 + RESET + "\n")

async def _sleep_with_tick_exits(total_seconds, tick_feed, position_manager, engine):
    """
    Sleeps for `total_seconds` (the REST poll interval), but when the
    WebSocket TickFeed is active, wakes every `tick_exit_check_interval_seconds`
    to run a tick-driven exit check against the feed's latest LTP — SL/T1/T2
    hits are caught between candle closes instead of waiting up to 60s
    (TASK-173, audit item 18). Falls back to a single plain sleep when the
    feed isn't active, identical to the pre-TASK-173 loop.
    """
    if not tick_feed.is_active:
        await asyncio.sleep(total_seconds)
        return

    interval = settings.tick_exit_check_interval_seconds
    elapsed = 0.0
    while elapsed < total_seconds:
        step = min(interval, total_seconds - elapsed)
        await asyncio.sleep(step)
        elapsed += step

        price = tick_feed.get_latest_price()
        if price is not None and position_manager.active_trades:
            try:
                events = await position_manager.update_trades(price)
                if any(ev_type == "SL_HIT" for _, ev_type in events):
                    engine.clear_cooldown()
            except Exception as e:
                print(f"[-] Tick-driven exit check failed: {e}")

async def _start_in_process_kronos_consumer():
    """
    Launches the Kronos ML live probability consumer as an in-process
    background task within main.py (TASK-186).
    """
    try:
        from ml_signal.config import MLConfig
        from ml_signal.kronos_consumer import KronosConsumer
        # Fresh config — mutating the shared DEFAULT_CONFIG singleton would
        # leak the webhook into every other MLConfig consumer.
        config = MLConfig()
        if getattr(settings, "discord_webhook_url", ""):
            config.discord_webhook_url = settings.discord_webhook_url
        # Profile is already applied by now, so this picks up the expiry-day 30
        # as well as the non-expiry 45 — the forecast must stop where the live
        # time stop trails the SL to entry, not 15 minutes past it.
        config.kronos_horizon_candles = settings.time_stop_minutes

        consumer = KronosConsumer(config)
        await consumer.run(
            supabase_url=settings.supabase_url,
            supabase_key=settings.supabase_key,
        )
    except Exception as kronos_err:
        print(f"{Y}[!] In-process Kronos consumer task exited: {kronos_err}{RESET}")


async def run():
    """
    Main entry point for the ARES Trading System.
    """
    # ── Step 0: Fetch Dhan credentials from Supabase ──
    load_dhan_credentials_from_supabase()

    # ── Step 1: Detect expiry day and apply config profile ──
    try:
        is_expiry = await is_expiry_day_from_api()
    except Exception:
        is_expiry = is_expiry_day_simple()

    if is_expiry:
        profile_name = "EXPIRY"
        settings.apply_profile(EXPIRY_CONFIG)
        print(f"{Y}{B}[ARES] 📅 EXPIRY DAY detected — applying aggressive config profile.{RESET}")
    else:
        profile_name = "NON-EXPIRY"
        settings.apply_profile(NON_EXPIRY_CONFIG)
        print(f"{G}{B}[ARES] 📅 Non-expiry day — applying standard config profile.{RESET}")

    engine = AresEngine()
    price_fetcher = PriceFetcher()
    oi_fetcher = OIFetcher()
    level_fetcher = LevelFetcher()
    storage = Storage()
    position_manager = PositionManager()
    ml_collector = MLCollector(settings.supabase_url, settings.supabase_key)
    tick_feed = TickFeed()

    # Kronos consumer deliberately NOT started (TASK-190). Measured peak RSS for one
    # forecast (context=1000, 20 sampled paths, horizon=45) is 3.6GB against ~710MB
    # usable on the 768mb VM. It OOM-killed the whole process — including the trading
    # loop — on every signal, and each kill cost a restart that zeroed VWAP and the
    # candle buffers, blinding ARES for the following 30 minutes.
    #
    # Shrinking does not rescue it: context 500 still peaks at 2.1GB, and 5 sampled
    # paths at 1.3GB. Inference costs ~160MB per path plus ~175MB fixed, so the ~370MB
    # of headroom left by the 340MB baseline buys exactly one path — a probability that
    # can only read 0% or 100%.
    #
    # The number was informational only (it never touched signal generation, entries,
    # or SL/T1/T2), so the trading loop keeps running without it. To restore it, give
    # Kronos its own machine and run `python -m ml_signal.kronos_consumer` there — do
    # not re-enable _start_in_process_kronos_consumer() on a shared 768mb VM.

    # Make this dynamic via Yahoo Finance Oracle 
    try:
        pdh, pdl = await price_fetcher.fetch_previous_day_ohlc()
    except Exception as e:
        print(f"{Y}[-] WARNING: Dynamic PDH/PDL fetch failed ({e}). Using last known safe defaults.{RESET}")
        pdh, pdl = 24100.0, 23900.0
        
    level_fetcher.set_previous_day_levels(high=pdh, low=pdl)
    
    # Initialize ML Data Collection Logger
    ml_table_ok = ml_collector.check_table_exists()
    
    print_banner(pdh, pdl, profile_name, ml_active=ml_table_ok)

    if ml_table_ok:
        print(f"{G}[+] ML Data Collection Logger: {B}ACTIVE{RESET} (recording 50+ features per cycle)")
    else:
        print(f"{Y}[!] ML Data Collection Logger: table 'ml_collection' not found{RESET}")
        print(f"{Y}    Run ml_signal/schema.sql in Supabase SQL Editor to enable.{RESET}")

    # WebSocket tick feed for exit monitoring (TASK-173, audit item 18).
    # Best-effort: on failure the loop just falls back to REST-only 60s
    # exit checks, exactly as before this feature existed.
    if tick_feed.start():
        print(f"{G}[+] Tick Feed       : {B}ACTIVE{RESET} (WebSocket exit checks every {settings.tick_exit_check_interval_seconds:.0f}s)")
    else:
        print(f"{Y}[!] Tick Feed       : unavailable — falling back to REST-only exit monitoring{RESET}")

    await send_startup_alert(pdh, pdl, profile_name, ml_active=ml_table_ok)

    prev_iv = None
    last_vwap_reset_date = None
    waiting_printed = False
    buffers_full_printed = False
    last_error_msg = None
    last_heartbeat_time = None
    
    while True:
        now = datetime.now()
        current_time = now.time()
        
        # Reset VWAP at 09:15 once per day
        if current_time >= time(9, 15) and now.date() != last_vwap_reset_date:
            price_fetcher.reset_vwap()
            last_vwap_reset_date = now.date()
            print(f"{C}[{now.strftime('%H:%M:%S')}] 🔄 VWAP reset for the new session.{RESET}")
            
        # Session gate: only run between 09:15 and 15:30
        if current_time >= time(15, 30):
            print(f"{G}[{now.strftime('%H:%M:%S')}] 🛑 Session ended. Shutting down to scale to zero...{RESET}")
            tick_feed.stop()
            break
            
        if current_time < time(9, 15):
            if not waiting_printed:
                print(f"{Y}[{now.strftime('%H:%M:%S')}] ⏸️ Pre-market (09:15 start). Sleeping...{RESET}")
                waiting_printed = True
            await asyncio.sleep(30)
            continue
            
        if waiting_printed:
            print(f"{G}[{now.strftime('%H:%M:%S')}] ▶️ Session Active. Starting market monitoring...{RESET}")
            waiting_printed = False
            
        try:
            # Fetch latest price candle
            candle = await price_fetcher.fetch_latest_candle()
            spot = candle.close
            
            # Fetch option chain dynamically
            expiry_date = await oi_fetcher.get_nearest_expiry()
            atm, full_chain = await oi_fetcher.fetch_chain(spot, expiry=expiry_date)
            
            # Build and update dynamic levels
            levels = level_fetcher.build_levels(
                spot_price=spot,
                full_chain=full_chain,
                oi_wall_threshold=settings.oi_wall_min_oi
            )
            
            # Compute IV change percentage
            current_iv = atm.ce.iv
            if prev_iv is None: 
                prev_iv = current_iv
            iv_change_pct = ((current_iv - prev_iv) / prev_iv) * 100.0 if prev_iv > 0 else 0.0
            prev_iv = current_iv
            
            # Run the engine
            signal = engine.tick(candle, full_chain, atm, iv_change_pct, levels, pdh, pdl)

            # ML Data Collection: log feature snapshot for every cycle
            ml_collector.snapshot(
                candle=candle,
                atm=atm,
                full_chain=full_chain,
                levels=levels,
                spot=spot,
                signal=signal,
                pdh=pdh,
                pdl=pdl,
                is_expiry=is_expiry,
                dte=None,
                timestamp=now,
            )

            # Terminal UI: Track Warmup State
            buffer_len = len(engine.candle_buffer)
            if buffer_len == settings.candle_buffer_size and not buffers_full_printed:
                print(f"{G}{B}[{now.strftime('%H:%M:%S')}] ✅ BUFFERS FULL: ARES is now actively scoring all setups.{RESET}", flush=True)
                buffers_full_printed = True
                
            # Heartbeat logging every 15 minutes
            if last_heartbeat_time is None or (now - last_heartbeat_time).total_seconds() >= 900:
                ml_stats = ml_collector.stats
                print(f"{C}[{now.strftime('%H:%M:%S')}] 💓 HEARTBEAT: Spot={spot:.2f} | Buffers={buffer_len}/{settings.candle_buffer_size} | ML Snapshots={ml_stats['total_snapshots']} (Signals: {ml_stats['signals_recorded']}){RESET}", flush=True)
                last_heartbeat_time = now
            
            # Process signal
            if signal:
                # Run options calculations (sizing, optimal strike selection)
                try:
                    await process_options_calculation(signal, full_chain, price_fetcher.dhan)
                except Exception as sizing_err:
                    print(f"{Y}[{now.strftime('%H:%M:%S')}] ⚠️ Option sizing calculation failed: {sizing_err}{RESET}")

                format_signal_console(signal, spot)
                try:
                    await storage.log_signal(signal, spot)
                except Exception as db_err:
                    print(f"{Y}[{now.strftime('%H:%M:%S')}] ⚠️ Database log failed: {db_err}{RESET}")

                # Every fired signal is a live trade now (TASK-182 removed the
                # observation-only gate).
                try:
                    position_manager.add_trade(signal, spot, atm=atm)
                except Exception as pm_err:
                    print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Position manager add_trade failed: {pm_err}{RESET}")

                try:
                    await send_discord(signal, spot)
                except Exception as alert_err:
                    print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Discord alert failed: {alert_err}{RESET}")

            # Update active trades with new spot price. Candle high/low enable
            # intrabar SL/target detection (TASK-172, audit item 11).
            try:
                trade_events = await position_manager.update_trades(
                    spot, candle_high=candle.high, candle_low=candle.low
                )
                # A stop-out frees the engine cooldown so the next setup can be
                # taken immediately instead of waiting out the timer.
                if any(ev_type == "SL_HIT" for _, ev_type in trade_events):
                    engine.clear_cooldown()
                    print(f"{Y}[{now.strftime('%H:%M:%S')}] 🔓 Cooldown cleared after stop-out — re-entry unlocked.{RESET}")
            except Exception as pm_update_err:
                print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Position manager update_trades failed: {pm_update_err}{RESET}")
            
            # Clear error tracking on successful cycle
            if last_error_msg is not None:
                print(f"{G}[{now.strftime('%H:%M:%S')}] ✅ Connection Recovered: Market data fetch successful.{RESET}")
                last_error_msg = None
                
        except Exception as e:
            error_str = str(e).lower()
            current_error = str(e)
            
            if "401" in error_str or "auth" in error_str:
                print(f"{R}[{now.strftime('%H:%M:%S')}] ❌ ERROR: Dhan API Authentication failed.{RESET}")
                print(f"   {W}Details: {e}{RESET}")
                print(f"   {W}Action : Reloading credentials from Supabase...{RESET}")
                
                try:
                    load_dhan_credentials_from_supabase()
                    from dhanhq import DhanContext, dhanhq
                    context = DhanContext(settings.dhan_client_id, settings.dhan_access_token)
                    price_fetcher.dhan = dhanhq(context)
                    oi_fetcher.dhan = dhanhq(context)
                    # Restart the WS feed too — it authenticated with the now-stale token.
                    tick_feed.stop()
                    tick_feed.start()
                    print(f"{G}   [+] Credentials reloaded successfully.{RESET}")
                except Exception as reload_err:
                    print(f"{R}   [!] Supabase credentials reload failed: {reload_err}{RESET}")
                
                print(f"   {Y}Retrying in 60s...\n{RESET}")
                
                if last_error_msg != current_error:
                    await send_error_alert(f"Dhan API Authentication failed: {e}")
                    last_error_msg = current_error
                    
                await asyncio.sleep(60)
            else:
                print(f"{Y}[{now.strftime('%H:%M:%S')}] ⚠️ Warning: Fetch cycle error - {e}{RESET}")
                if last_error_msg != current_error:
                    await send_error_alert(f"Fetch cycle error - {e}")
                    last_error_msg = current_error
            
        await _sleep_with_tick_exits(settings.poll_interval_seconds, tick_feed, position_manager, engine)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n⏹️ ARES monitoring stopped by user.", flush=True)
    except Exception as fatal_error:
        print(f"\n{R}🚨 FATAL ERROR: ARES crashed! {fatal_error}{RESET}", flush=True)
        try:
            asyncio.run(send_error_alert(f"FATAL SYSTEM CRASH: {fatal_error}"))
        except:
            pass