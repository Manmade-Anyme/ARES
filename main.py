import asyncio
from datetime import datetime, time

from engine import AresEngine
from fetchers.price_fetcher import PriceFetcher
from fetchers.oi_fetcher import OIFetcher
from fetchers.level_fetcher import LevelFetcher
from storage import Storage
from position_manager import PositionManager
from config import settings
from config_profiles import EXPIRY_CONFIG, NON_EXPIRY_CONFIG
from detectors.expiry_detector import is_expiry_day_from_api, is_expiry_day_simple
from alerts import send_discord, send_startup_alert, send_error_alert

# ANSI Color Codes for Premium Terminal UI
G = "\033[92m"  # Green
Y = "\033[93m"  # Yellow
R = "\033[91m"  # Red
C = "\033[96m"  # Cyan
B = "\033[1m"   # Bold
W = "\033[97m"  # White
RESET = "\033[0m"

def print_banner(pdh: float, pdl: float, profile_name: str = "DEFAULT"):
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
    print(f"   {W}Reasons:{RESET}")
    for r in signal.reasons:
        print(f"     {W}• {r}{RESET}")
    print(f"{color}{B}━" * 65 + RESET + "\n")

async def run():
    """
    Main entry point for the ARES Trading System.
    """
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
    
    # Make this dynamic via Yahoo Finance Oracle 
    try:
        pdh, pdl = await price_fetcher.fetch_previous_day_ohlc()
    except Exception as e:
        print(f"{Y}[-] WARNING: Dynamic PDH/PDL fetch failed ({e}). Using last known safe defaults.{RESET}")
        pdh, pdl = 24100.0, 23900.0
        
    level_fetcher.set_previous_day_levels(high=pdh, low=pdl)
    
    print_banner(pdh, pdl, profile_name)
    await send_startup_alert(pdh, pdl, profile_name)

    
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
        if not (time(9, 15) <= current_time <= time(15, 30)):
            if not waiting_printed:
                print(f"{Y}[{now.strftime('%H:%M:%S')}] ⏸️ Outside session hours (09:15 - 15:30). Sleeping...{RESET}")
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
            signal = engine.tick(candle, full_chain, atm, iv_change_pct, levels)
            
            # Terminal UI: Track Warmup State
            buffer_len = len(engine.candle_buffer)
            if buffer_len == settings.candle_buffer_size and not buffers_full_printed:
                print(f"{G}{B}[{now.strftime('%H:%M:%S')}] ✅ BUFFERS FULL: ARES is now actively scoring all setups.{RESET}", flush=True)
                buffers_full_printed = True
                
            # Heartbeat logging every 15 minutes
            if last_heartbeat_time is None or (now - last_heartbeat_time).total_seconds() >= 900:
                print(f"{C}[{now.strftime('%H:%M:%S')}] 💓 HEARTBEAT: ARES Engine active | Spot: {spot:.2f} | Buffers: {buffer_len}/{settings.candle_buffer_size}{RESET}", flush=True)
                last_heartbeat_time = now
            
            # Process signal
            if signal:
                format_signal_console(signal, spot)
                try:
                    await storage.log_signal(signal, spot)
                except Exception as db_err:
                    print(f"{Y}[{now.strftime('%H:%M:%S')}] ⚠️ Database log failed: {db_err}{RESET}")
                
                try:
                    position_manager.add_trade(signal, spot, atm=atm)
                except Exception as pm_err:
                    print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Position manager add_trade failed: {pm_err}{RESET}")
                
                try:
                    await send_discord(signal, spot)
                except Exception as alert_err:
                    print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Discord alert failed: {alert_err}{RESET}")
            
            # Update active trades with new spot price
            try:
                await position_manager.update_trades(spot)
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
                print(f"   {W}Action : Check your DHAN_ACCESS_TOKEN in the .env file.{RESET}")
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
            
        await asyncio.sleep(settings.poll_interval_seconds)

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