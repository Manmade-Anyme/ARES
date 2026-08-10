import httpx
from datetime import datetime, timezone, timedelta
from models import AresSignal
from config import settings, detector_names, SESSION_DISPLAY

def format_signal(signal: AresSignal, spot: float) -> str:
    """
    Formats the AresSignal into a premium, color-coded Discord message using diff blocks.
    """
    reasons_str = "\n".join([f"     • {r}" for r in signal.reasons])
    
    # Use diff block markers for color coding (+ for green/bullish, - for red/bearish)
    marker = "+" if signal.direction.value == "BULLISH" else "-"
    emoji = "🚨 🐂 🟢" if signal.direction.value == "BULLISH" else "🚨 🐻 🔴"
    
    # Get current IST time
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%d-%b-%Y %H:%M:%S")

    sizing_str = ""
    if getattr(signal, "suggested_lots", None) is not None:
        sizing_str = f"""

   📐 Option Sizing Calculator (Risk: {signal.risk_pct:.1f}%) -
   🔢 Lots   : **{signal.suggested_lots}** (Nifty Lot Size: {settings.nifty_lot_size})
   ✅ Entry : **₹ {signal.option_premium:.2f}** (Delta: {signal.option_delta:+.4f})
   🛑 SL  : **₹ {signal.option_sl:.2f}**
   🎯 Target : **₹ {signal.option_target:.2f}**"""

    ml_pred_str = ""
    if hasattr(signal, "ml_prediction") and signal.ml_prediction:
        prob = int(signal.ml_prediction.get("probability", 0) * 100)
        version = signal.ml_prediction.get("model_version", "v1")
        ml_pred_str = f"\n\n   🤖 ML Prediction: {prob}% (proxy model, {version})"

    msg = f"""```diff
{marker} {emoji} #{getattr(signal, 'signal_id', '0000')} SIGNAL DETECTED: {signal.setup_type.value} ({signal.direction.value})
```
   🕒 Time  : {now_ist} IST
   📍 Spot  : {spot:.2f}
   ✅ Entry : **{signal.entry_zone[0]:.2f} - {signal.entry_zone[1]:.2f}**
   🛑 SL    : **{signal.stop_loss:.2f} **(Spot Ref)
   🎯 Target: **T1={signal.target_1:.2f} | T2={signal.target_2:.2f}**
   ⚡ Trade : **{signal.strike_to_trade} {signal.option_type}**
   ⭐ Confidence : {signal.confidence}{sizing_str}{ml_pred_str}

   📝 Reasons:
{reasons_str}"""
    return msg

async def send_discord(signal: AresSignal, spot: float) -> None:
    """
    Formats the signal and sends it asynchronously to the configured Discord webhook using Embed Fields.
    """
    if not settings.discord_webhook_url:
        return
        
    is_bullish = signal.direction.value == "BULLISH"
    webhook_url = settings.discord_webhook_url

    # Get current IST time
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%d-%b-%Y %H:%M:%S")

    color = 3066993 if is_bullish else 15158332  # Green or Red
    emoji = "🚨 🐂 🟢" if is_bullish else "🚨 🐻 🔴"
    title = f"{emoji} #{getattr(signal, 'signal_id', '0000')} SIGNAL DETECTED: {signal.setup_type.value} ({signal.direction.value})"

    # Base fields
    fields = [
        {"name": "🕒 Time", "value": f"{now_ist} IST", "inline": False},
        {"name": "📍 Spot", "value": f"**{spot:.2f}**", "inline": True},
        {"name": "⚡ Trade", "value": f"**{signal.strike_to_trade} {signal.option_type}**", "inline": True},
        {"name": "⭐ Confidence", "value": f"**{signal.confidence}**", "inline": True},
        {"name": "✅ Entry", "value": f"**{signal.entry_zone[0]:.2f} - {signal.entry_zone[1]:.2f}**", "inline": True},
        {"name": "🛑 SL", "value": f"**{signal.stop_loss:.2f}** (Spot Ref)", "inline": True},
        {"name": "🎯 Target", "value": f"**T1={signal.target_1:.2f} | T2={signal.target_2:.2f}**", "inline": True}
    ]

    # Sizing fields
    if getattr(signal, "suggested_lots", None) is not None:
        fields.append({"name": f"📐 Option Sizing Calculator (Risk: {signal.risk_pct:.1f}%)", "value": "Calculations based on current capital", "inline": False})
        fields.append({"name": "🔢 Lots", "value": f"**{signal.suggested_lots}** (Nifty Lot Size: {settings.nifty_lot_size})", "inline": True})
        fields.append({"name": "✅ Option Entry", "value": f"**₹ {signal.option_premium:.2f}** (Delta: {signal.option_delta:+.4f})", "inline": True})
        fields.append({"name": "🛑 Option SL", "value": f"**₹ {signal.option_sl:.2f}**", "inline": True})
        fields.append({"name": "🎯 Option Target", "value": f"**₹ {signal.option_target:.2f}**", "inline": True})

    # ML Prediction
    if hasattr(signal, "ml_prediction") and signal.ml_prediction:
        prob = int(signal.ml_prediction.get("probability", 0) * 100)
        version = signal.ml_prediction.get("model_version", "v1")
        fields.append({"name": "🤖 ML Prediction", "value": f"**{prob}%** (proxy model, {version})", "inline": False})

    # Reasons
    reasons_str = "\n".join([f"• {r}" for r in signal.reasons])
    fields.append({"name": "📝 Reasons", "value": reasons_str, "inline": False})

    payload = {
        "embeds": [
            {
                "title": title,
                "color": color,
                "fields": fields
            }
        ]
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord signal alert failed: {type(e).__name__} - {e}")

async def send_startup_alert(
    pdh: float,
    pdl: float,
    profile_name: str = "DEFAULT",
    ml_active: bool = False,
    predictor_active: bool = False,
    predictor_model_name: str = "v1.joblib",
) -> None:
    """
    Sends a startup message to Discord with the current PDH/PDL and status.

    Mirrors main.print_banner. The detector list and session string come from
    config so the two renderings cannot drift apart again — this copy had been
    advertising a 23:30 session and omitting Trend Continuation.
    """
    webhook_url = settings.discord_webhook_url
    if not webhook_url:
        return

    # No leading "+ " on these values — the template already prefixes every line
    # with it. Carrying it here too rendered "+ + [+] ML Data Collection ...".
    ml_line = "ACTIVE (recording 50+ features per cycle)" if ml_active else "inactive (table not found)"
    predictor_line = f"ACTIVE ({predictor_model_name})" if predictor_active else "inactive (model not loaded)"

    msg = f"""```diff
+ =================================================================
+ 🤖 ARES (Adaptive Reversal & Entry Signal) - Initialization
+ =================================================================
+ [+] Config       : {profile_name} DAY PROFILE
+ [+] Target Asset : {settings.yahoo_symbol} (1-minute timeframe)
+ [+] Detectors    : {', '.join(detector_names())}
+ [+] Session      : {SESSION_DISPLAY}
+ [+] Cooldown     : {settings.signal_cooldown_minutes} minutes between signals
+ [+] PDH / PDL    : {pdh:.2f} / {pdl:.2f}
+ [+] ML Collection: {ml_line}
+ [+] ML Predictor : {predictor_line}
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

def trade_update_action_text(update_type: str, trade: dict) -> str:
    """The human sentence describing one trade state change.

    Pure and exhaustive on purpose. TASK-198 added STOPPED_OUT_AT_BE to
    position_manager but not here, so the if/elif chain this replaces fell
    through and rendered "⚡ Action : ****" in the live Discord embed for every
    break-even exit. The final fallback means a state added to position_manager
    in future degrades to a readable line instead of a blank one.
    """
    if update_type == "T1_HIT":
        return "Target 1 Reached! Stop Loss trailed to Entry."
    if update_type == "T2_HIT":
        return "Target 2 Reached! Trade Closed with Full Profit."
    if update_type == "STOPPED_OUT_AT_BE":
        return "Trailing Stop Loss Hit at Entry. Trade Closed at Break-even."
    if update_type == "TIME_STOP":
        return "Time-Stop: SL Trailed to Entry Hit. Trade Closed."
    if update_type == "SL_HIT":
        # Pre-TASK-198 rows can still reach here with a trailed stop; keep
        # reporting those as the break-even exit they were.
        if trade.get("state") == "T1_HIT" or trade.get("stop_loss") == trade.get("entry_price"):
            return "Trailing Stop Loss Hit at Entry. Trade Closed."
        return "Stop Loss Hit. Trade Closed."
    return f"Trade Closed ({update_type})."


async def send_trade_update(trade: dict, spot: float, update_type: str) -> None:
    """
    Sends an alert when an active trade state changes using embeds (e.g., T1 Hit, Trailing Stop triggered, SL Hit).
    """
    if not settings.discord_webhook_url:
        return

    # Color code based on direction
    is_bullish = trade.get("direction") == "BULLISH"
    color = 3066993 if is_bullish else 15158332  # Green or Red
    icon = "🚨 🐂 🟢" if is_bullish else "🚨 🐻 🔴"
    
    action_text = trade_update_action_text(update_type, trade)

    # Get current IST time
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%d-%b-%Y %H:%M:%S")

    description = f"""
🕒 **Time**    : {now_ist} IST
📍 **Spot**    : **{spot:.2f}**
⚡ **Action**  : **{action_text}**
✅ **Entry**   : **{trade['entry_price']:.2f}**
🛑 **New SL**  : **{trade['stop_loss']:.2f}**
⭐ **Status**  : **{trade['state']}**
"""

    payload = {
        "embeds": [
            {
                "title": f"{icon} #{trade.get('signal_id', '0000')} TRADE UPDATE: {trade['setup_type']} ({trade['direction']})",
                "color": color,
                "description": description
            }
        ]
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(settings.discord_webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord trade update alert failed: {type(e).__name__} - {e}")
