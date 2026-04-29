import asyncio
from datetime import datetime, time

from engine import AresEngine
from fetchers.price_fetcher import PriceFetcher
from fetchers.oi_fetcher import OIFetcher
from fetchers.level_fetcher import LevelFetcher
from storage import Storage
from config import settings
from alerts import send_discord, send_startup_alert, send_error_alert

def print_banner(pdh: float, pdl: float):
    """Prints the ARES startup banner with configuration details."""
    print("=" * 65)
    print("  ARES (Adaptive Reversal & Entry Signal) - Initialization")
    print("=" * 65)
    print(f"[+] Target Asset : {settings.yahoo_symbol} (1-minute timeframe)")
    print(f"[+] Detectors    : Failed Breakout, OI Wall, Exhaustion")
    print(f"[+] Session      : 09:15 to 23:30 IST")
    print(f"[+] Cooldown     : {settings.signal_cooldown_minutes} minutes between signals")
    print(f"[+] PDH / PDL    : {pdh} / {pdl}")
    print("=" * 65)

def format_signal_console(signal, spot):
    """Formats and prints a detailed signal alert to the console."""
    print("\n" + "━" * 65)
    print(f"🚨 SIGNAL DETECTED: {signal.setup_type.value} ({signal.direction.value})")
    print(f"   Spot  : {spot:.2f}")
    print(f"   Trade : {signal.strike_to_trade} {signal.option_type}")
    print(f"   Entry : {signal.entry_zone[0]:.2f} - {signal.entry_zone[1]:.2f}")
    print(f"   SL    : {signal.stop_loss:.2f} (Spot Ref)")
    print(f"   Targets: T1={signal.target_1:.2f} | T2={signal.target_2:.2f}")
    print(f"   Confidence: {signal.confidence}")
    print("   Reasons:")
    for r in signal.reasons:
        print(f"     • {r}")
    print("━" * 65 + "\n")

async def run():
    """
    Main entry point for the ARES Trading System.
    """
    engine = AresEngine()
    price_fetcher = PriceFetcher()
    oi_fetcher = OIFetcher()
    level_fetcher = LevelFetcher()
    storage = Storage()
    
    # Make this dynamic via Yahoo Finance Oracle 
    try:
        pdh, pdl = await price_fetcher.fetch_previous_day_ohlc()
    except Exception as e:
        print(f"[-] WARNING: Dynamic PDH/PDL fetch failed ({e}). Using last known safe defaults.")
        pdh, pdl = 24100.0, 23900.0
        
    level_fetcher.set_previous_day_levels(high=pdh, low=pdl)
    
    print_banner(pdh, pdl)
    await send_startup_alert(pdh, pdl)
    
    prev_iv = None
    last_vwap_reset_date = None
    waiting_printed = False
    buffers_full_printed = False
    
    while True:
        now = datetime.now()
        current_time = now.time()
        
        # Reset VWAP at 09:15 once per day
        if current_time >= time(9, 15) and now.date() != last_vwap_reset_date:
            price_fetcher.reset_vwap()
            last_vwap_reset_date = now.date()
            print(f"[{now.strftime('%H:%M:%S')}] 🔄 VWAP reset for the new session.")
            
        # Session gate: only run between 09:15 and 23:30
        if not (time(9, 15) <= current_time <= time(23, 30)):
            if not waiting_printed:
                print(f"[{now.strftime('%H:%M:%S')}] ⏸️ Outside session hours (09:15 - 23:30). Sleeping...")
                waiting_printed = True
            await asyncio.sleep(30)
            continue
            
        if waiting_printed:
            print(f"[{now.strftime('%H:%M:%S')}] ▶️ Session Active. Starting market monitoring...")
            waiting_printed = False
            
        try:
            # Fetch latest price candle
            candle = await price_fetcher.fetch_latest_candle()
            spot = candle.close
            
            # Fetch option chain dynamically
            expiry_date = await oi_fetcher.get_nearest_expiry()
            atm, full_chain = await oi_fetcher.fetch_chain(spot, expiry=expiry_date)
            
            # Build and update dynamic levels
            engine.breakout_detector.levels = level_fetcher.build_levels(
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
            signal = engine.tick(candle, full_chain, atm, iv_change_pct)
            
            # Terminal UI: Track Warmup State
            buffer_len = len(engine.candle_buffer)
            if buffer_len == settings.candle_buffer_size and not buffers_full_printed:
                print(f"[{now.strftime('%H:%M:%S')}] ✅ BUFFERS FULL: ARES is now actively scoring all setups.")
                buffers_full_printed = True
            
            # Process signal
            if signal:
                format_signal_console(signal, spot)
                try:
                    await storage.log_signal(signal, spot)
                except Exception as db_err:
                    print(f"[{now.strftime('%H:%M:%S')}] ⚠️ Database log failed: {db_err}")
                
                try:
                    await send_discord(signal, spot)
                except Exception as alert_err:
                    print(f"[{now.strftime('%H:%M:%S')}] ⚠️ Discord alert failed: {alert_err}")
                
        except Exception as e:
            error_str = str(e).lower()
            if "401" in error_str or "auth" in error_str:
                print(f"\n[{now.strftime('%H:%M:%S')}] ❌ ERROR: Dhan API Authentication failed.")
                print(f"   Details: {e}")
                print(f"   Action : Check your DHAN_ACCESS_TOKEN in the .env file.")
                print(f"   Retrying in 60s...\n")
                await send_error_alert(f"Dhan API Authentication failed: {e}")
                await asyncio.sleep(60)
            else:
                print(f"[{now.strftime('%H:%M:%S')}] ⚠️ Warning: Fetch cycle error - {e}")
                await send_error_alert(f"Fetch cycle error - {e}")
            
        await asyncio.sleep(settings.poll_interval_seconds)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n⏹️ ARES monitoring stopped by user.")