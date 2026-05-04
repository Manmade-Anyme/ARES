import httpx
from datetime import datetime, timezone, timedelta
from models import AresSignal, SetupType
from config import settings

def format_signal(signal: AresSignal, spot: float) -> str:
    """
    Formats the AresSignal into a premium, color-coded Discord message using diff blocks.
    """
    reasons_str = "\n".join([f"     • {r}" for r in signal.reasons])
    
    # Use diff block markers for color coding (+ for green/bullish, - for red/bearish)
    marker = "+" if signal.direction.value == "BULLISH" else "-"
    icon = "📈" if signal.direction.value == "BULLISH" else "📉"
    
    msg = f"""```diff
{marker} {icon} SIGNAL DETECTED: {signal.setup_type.value} ({signal.direction.value})
   
   📍 Spot  : {spot:.2f}
   ⚡ Trade : {signal.strike_to_trade} {signal.option_type}
   ✅ Entry : {signal.entry_zone[0]:.2f} - {signal.entry_zone[1]:.2f}
   🛑 SL    : {signal.stop_loss:.2f} (Spot Ref)
   🎯 Target: T1={signal.target_1:.2f} | T2={signal.target_2:.2f}
   ⭐ Conf. : {signal.confidence}
   
   📝 Reasons:
{reasons_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```"""
    return msg

async def send_discord(signal: AresSignal, spot: float) -> None:
    """
    Formats the signal and sends it asynchronously to the configured Discord webhook.
    """
    if not settings.discord_webhook_url:
        return
        
    content = format_signal(signal, spot)
    payload = {"content": content}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(settings.discord_webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord signal alert failed: {type(e).__name__} - {e}")

async def send_startup_alert(pdh: float, pdl: float) -> None:
    """
    Sends a startup message to Discord with the current PDH/PDL and status.
    """
    webhook_url = settings.discord_health_webhook_url or settings.discord_webhook_url
    if not webhook_url:
        return
        
    msg = f"""```diff
+ =================================================================
+ 🤖 ARES (Adaptive Reversal & Entry Signal) - Initialization
+ =================================================================
+ [+] Target Asset : {settings.yahoo_symbol} (1-minute timeframe)
+ [+] Detectors    : Failed Breakout, OI Wall, Exhaustion
+ [+] Session      : 09:15 to 23:30 IST
+ [+] Cooldown     : {settings.signal_cooldown_minutes} minutes between signals
+ [+] PDH / PDL    : {pdh:.2f} / {pdl:.2f}
+ =================================================================
```"""

    payload = {"content": msg}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord startup alert failed: {type(e).__name__} - {e}")

async def send_error_alert(error_msg: str) -> None:
    """
    Sends a system alert regarding errors to Discord.
    """
    webhook_url = settings.discord_health_webhook_url or settings.discord_webhook_url
    if not webhook_url:
        return
        
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%H:%M:%S")
    
    msg = f"""```diff
- 🚨 SYSTEM ALERT — ACTION REQUIRED
- ──────────────────────────────────
- ❌ Error  : {error_msg}
- 🕒 Time   : {now_ist} IST
-
- 🛠️ Action : Check logs and restart system.
- ──────────────────────────────────
```"""

    payload = {"content": msg}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord error alert failed: {type(e).__name__} - {e}")

async def send_heartbeat(spot: float, buffer_len: int) -> None:
    """
    Sends a periodic heartbeat to Discord health channel.
    """
    webhook_url = settings.discord_health_webhook_url or settings.discord_webhook_url
    if not webhook_url:
        return
        
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%H:%M:%S")
    
    msg = f"""```diff
+ 💓 HEARTBEAT: ARES Engine Active
+ ──────────────────────────────────
+ 📍 Spot    : {spot:.2f}
+ 📊 Buffers : {buffer_len}/{settings.candle_buffer_size}
+ 🕒 Time    : {now_ist} IST
+ ──────────────────────────────────
```"""

    payload = {"content": msg}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord heartbeat alert failed: {type(e).__name__} - {e}")

async def send_trade_update(trade: dict, spot: float, update_type: str) -> None:
    """
    Sends an alert when an active trade state changes (e.g., T1 Hit, Trailing Stop triggered, SL Hit).
    """
    if not settings.discord_webhook_url:
        return

    # Color code based on direction and update type
    color_marker = "+" if update_type == "T1_HIT" else "-"
    icon = "🎯" if update_type == "T1_HIT" else "🛑"
    
    action_text = ""
    if update_type == "T1_HIT":
        action_text = "Target 1 Reached! Stop Loss trailed to Entry."
    elif update_type == "SL_HIT":
        if trade["state"] == "T1_HIT" or trade.get("stop_loss") == trade.get("entry_price"):
            action_text = "Trailing Stop Loss Hit at Entry. Trade Closed."
        else:
            action_text = "Stop Loss Hit. Trade Closed."

    msg = f"""```diff
{color_marker} {icon} TRADE UPDATE: {trade['setup_type']} ({trade['direction']})
   
   📍 Spot    : {spot:.2f}
   ⚡ Action  : {action_text}
   ✅ Entry   : {trade['entry_price']:.2f}
   🛑 New SL  : {trade['stop_loss']:.2f}
   📊 Status  : {trade['state']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```"""

    payload = {"content": msg}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(settings.discord_webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord trade update alert failed: {type(e).__name__} - {e}")
