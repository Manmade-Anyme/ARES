import httpx
import math
from datetime import datetime, timezone, timedelta
from models import AresSignal, OIWallBias, OIWallWatchCancellation
from config import settings, detector_names, SESSION_DISPLAY


def _number(value, precision=".2f"):
    """Render unavailable quote/sizing data without inventing a numeric value."""
    if isinstance(value, bool):
        return "unavailable"
    try:
        number = float(value)
        return format(number, precision) if math.isfinite(number) else "unavailable"
    except (TypeError, ValueError, OverflowError):
        return "unavailable"


def _option_bracket_fields(signal, *, for_discord=False):
    """Show calculated option values independently of order execution eligibility."""
    bracket = getattr(signal, "bracket", None)
    status = getattr(signal, "options_sizing_status", "PENDING")
    if bracket is None and status == "PENDING":
        return []
    if bracket is None or bracket.greeks is None:
        reason = getattr(bracket, "rejection_reason", None) or status
        return [{"name": "📐 Option Sizing", "value": f"Option prices unavailable ({reason}).", "inline": False}]
    greeks = bracket.greeks
    lot_size = getattr(bracket, "lot_size", settings.nifty_lot_size)
    context = (getattr(signal, "market_context", None) or {}).get("options_sizing", {})
    uncapped = context.get("uncapped_suggested_lots", getattr(bracket, "uncapped_suggested_lots", None))
    capital, risk_pct = signal.capital, signal.risk_pct
    risk = None
    if _number(capital) != "unavailable" and _number(risk_pct) != "unavailable":
        risk = max(0, float(capital)) * max(0, float(risk_pct)) / 100
    fields = [
        {"name": "📄 Contract", "value": f"{bracket.security_id} | Expiry {bracket.expiry_date}", "inline": True},
        {"name": "✅ Option Entry", "value": f"₹ {_number(bracket.entry_limit)} | LTP: ₹ {_number(signal.option_premium)}", "inline": True},
        {"name": "📊 Option Greeks", "value": f"Δ {_number(greeks.delta, '+.3f')} | Γ {_number(greeks.gamma, '.4f')} | Θ {_number(greeks.theta, '.1f')}/day | Vega {_number(greeks.vega)} | IV {_number(greeks.iv, '.1f')}%", "inline": False},
        {"name": "🎯 Target 1", "value": f"₹ {_number(bracket.target_1)} (Spot: {_number(signal.target_1)})", "inline": True},
        {"name": "🎯 Target 2", "value": f"₹ {_number(bracket.target_2)} (Reference | Spot: {_number(signal.target_2)})", "inline": True},
        {"name": "🛑 Stop Loss", "value": f"₹ {_number(bracket.catastrophic_opt_sl)}" if for_discord else f"Layer 1 (Synthetic Invalidation): Spot {_number(bracket.synthetic_spot_sl)}\nLayer 2 (Broker Emergency Floor): ₹ {_number(bracket.catastrophic_opt_sl)}", "inline": False},
        {"name": "📐 Sizing", "value": f"{bracket.lots} Lots ({bracket.lots * lot_size} qty)", "inline": True},
        {"name": "💰 Risk Budget", "value": f"₹{_number(risk, '.0f')} ({_number(risk_pct, '.1f')}% of ₹{_number(capital, '.0f')} | Uncapped Suggestion: {_number(uncapped, '.0f')} lots)", "inline": False},
    ]
    if for_discord and not settings.discord_show_option_details:
        fields = [field for field in fields if field["name"] not in {
            "📊 Option Greeks", "💰 Risk Budget",
        }]
    if getattr(bracket, "iv_feed_warning", None):
        fields.append({"name": "⚠️ IV Diagnostic", "value": f"{bracket.iv_feed_warning} (diagnostic only)", "inline": False})
    return fields



def _format_wall_event_time(raw_timestamp):
    if not isinstance(raw_timestamp, str):
        return None
    try:
        event_time = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if event_time.tzinfo is None:
        return None
    return event_time.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d-%b-%Y %H:%M IST")


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
    bracket_fields = _option_bracket_fields(signal)
    if bracket_fields:
        sizing_str = "\n\n" + "\n".join(f"   {field['name']}: {field['value']}" for field in bracket_fields)
    elif getattr(signal, "suggested_lots", None) is not None:
        sizing_str = f"""

   📐 Option Sizing Calculator (Risk: {_number(signal.risk_pct, '.1f')}%) -
   🔢 Lots   : **{signal.suggested_lots}** (Nifty Lot Size: {settings.nifty_lot_size})
   ✅ Entry : **₹ {_number(signal.option_premium)}** (Delta: {_number(signal.option_delta, '+.4f')})
   🛑 SL  : **₹ {_number(signal.option_sl)}**
   🎯 Target : **₹ {_number(signal.option_target)}**"""

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

async def send_discord(signal: AresSignal, spot: float) -> bool:
    """
    Formats the signal and sends it asynchronously to the configured Discord webhook using Embed Fields.
    """
    if not settings.discord_webhook_url:
        return False
        
    is_bullish = signal.direction.value == "BULLISH"
    webhook_url = settings.discord_webhook_url

    # Get current IST time
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%d-%b-%Y %H:%M:%S")

    color = 3066993 if is_bullish else 15158332  # Green or Red
    emoji = "🚨 🐂 🟢" if is_bullish else "🚨 🐻 🔴"
    title = f"{emoji} #{getattr(signal, 'signal_id', '0000')} SIGNAL DETECTED: {signal.setup_type.value} ({signal.direction.value})"

    sl_val = getattr(signal, "stop_loss", 0.0)
    sl_value = f"**{sl_val:.2f}** (Spot Ref)" if isinstance(sl_val, (int, float)) else f"**{sl_val}** (Spot Ref)"
    oi_ctx = getattr(signal, "oi_wall_context", None)
    if (
        isinstance(oi_ctx, dict)
        and isinstance(oi_ctx.get("wall_strike"), (int, float))
        and isinstance(sl_val, (int, float))
    ):
        w_strike = float(oi_ctx["wall_strike"])
        sl_diff = abs(sl_val - w_strike)
        sl_value = f"**{sl_val:.2f}** (Spot Ref, {sl_diff:.1f} pts from wall {w_strike:.0f})"

    # Base fields
    fields = [
        {"name": "🕒 Time", "value": f"{now_ist} IST", "inline": False},
        {"name": "📍 Spot", "value": f"**{spot:.2f}**", "inline": True},
        {"name": "⚡ Trade", "value": f"**{signal.strike_to_trade} {signal.option_type}**", "inline": True},
        {"name": "⭐ Confidence", "value": f"**{signal.confidence}**", "inline": True},
        {"name": "✅ Entry", "value": f"**{signal.entry_zone[0]:.2f} - {signal.entry_zone[1]:.2f}**", "inline": True},
        {"name": "🛑 SL", "value": sl_value, "inline": True},
        {"name": "🎯 Target", "value": f"**T1={signal.target_1:.2f} | T2={signal.target_2:.2f}**", "inline": True}
    ]

    # OI Wall Context
    if (
        isinstance(oi_ctx, dict)
        and isinstance(oi_ctx.get("wall_strike"), (int, float))
    ):
        w_strike = float(oi_ctx["wall_strike"])
        w_opt = str(oi_ctx.get("wall_option_type", ""))
        raw_oi = oi_ctx.get("wall_oi")
        w_oi = (float(raw_oi) if isinstance(raw_oi, (int, float)) else 0.0) / 100000.0
        raw_change = oi_ctx.get("wall_oi_change_pct")
        w_change = float(raw_change) if isinstance(raw_change, (int, float)) else 0.0
        raw_snaps = oi_ctx.get("persistence_snapshots")
        snaps = int(raw_snaps) if isinstance(raw_snaps, (int, float)) else 0
        wall_text = f"**{w_strike:.0f} {w_opt}** ({w_oi:.1f}L contracts, +{w_change:.1f}%) | {snaps}/3 snapshots persistent"
        fields.append({"name": "🛡️ Wall Context", "value": wall_text, "inline": False})

        retest_time = _format_wall_event_time(oi_ctx.get("retest_timestamp"))
        if retest_time:
            retest_text = (
                f"At **{retest_time}**, spot closed **{signal.trigger_price:.2f}** "
                f"near **{w_strike:.0f} {w_opt}**."
            )
            interaction_time = _format_wall_event_time(oi_ctx.get("initial_interaction_timestamp"))
            if interaction_time:
                retest_text += f"\nInitial wall interaction: **{interaction_time}**"
            excursion = oi_ctx.get("favourable_excursion_pts")
            if isinstance(excursion, (int, float)):
                retest_text += f" | Favourable excursion: **{excursion:.2f} pts**"
            fields.append({"name": "🔁 Retest Confirmation", "value": retest_text, "inline": False})

    # Sizing fields
    bracket_fields = _option_bracket_fields(signal, for_discord=True)
    if bracket_fields:
        fields.extend(bracket_fields)
    elif getattr(signal, "suggested_lots", None) is not None:
        fields.append({"name": f"📐 Option Sizing Calculator (Risk: {_number(signal.risk_pct, '.1f')}%)", "value": "Calculations based on current capital", "inline": False})
        fields.append({"name": "🔢 Lots", "value": f"**{signal.suggested_lots}** (Nifty Lot Size: {settings.nifty_lot_size})", "inline": True})
        fields.append({"name": "✅ Option Entry", "value": f"**₹ {_number(signal.option_premium)}** (Delta: {_number(signal.option_delta, '+.4f')})", "inline": True})
        fields.append({"name": "🛑 Option SL", "value": f"**₹ {_number(signal.option_sl)}**", "inline": True})
        fields.append({"name": "🎯 Option Target", "value": f"**₹ {_number(signal.option_target)}**", "inline": True})

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

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        return True
    except Exception as e:
        print(f"[-] Discord signal alert failed: {type(e).__name__} - {e}")
        return False


def format_watchlist_alert(bias: OIWallBias, spot: float) -> str:
    """
    Formats the watchlist alert text for console or dry-run testing.
    """
    wall_oi_lakhs = bias.wall_oi / 100000.0
    return (
        f"🛡️ 🟡 #{bias.wall_key} SETUP WATCH: OI_WALL_RETEST_READY ({bias.direction.value})\n"
        f"Spot: {spot:.2f} | Wall: {bias.wall_strike:.0f} {bias.wall_option_type} "
        f"({wall_oi_lakhs:.1f}L, +{bias.wall_oi_change_pct:.1f}%) | "
        f"Persistence: {bias.persistence_snapshots} snapshots\n"
        f"Guidance: Awaiting pullback re-test near {bias.wall_strike:.0f}. Do NOT chase breakdown/bounce."
    )


def build_watchlist_payload(bias: OIWallBias, spot: float, observed_at=None) -> dict:
    """Freeze the original ready-watch embed without changing its fields."""
    ist = timezone(timedelta(hours=5, minutes=30))
    timestamp = observed_at or datetime.now(ist)
    timestamp = timestamp.replace(tzinfo=ist) if timestamp.tzinfo is None else timestamp.astimezone(ist)
    now_ist = timestamp.strftime("%d-%b-%Y %H:%M:%S")

    wall_oi_lakhs = bias.wall_oi / 100000.0
    direction_desc = bias.direction.value
    header = f"🛡️ 🟡 #{bias.wall_key} SETUP WATCH: OI_WALL_RETEST_READY ({direction_desc})"

    fields = [
        {"name": "🕒 Time", "value": f"{now_ist} IST", "inline": True},
        {"name": "📍 Current Spot", "value": f"**{spot:.2f}**", "inline": True},
        {"name": "🛡️ Wall Barrier", "value": f"**{bias.wall_strike:.0f} {bias.wall_option_type}** ({wall_oi_lakhs:.1f}L contracts, +{bias.wall_oi_change_pct:.1f}%)", "inline": False},
        {"name": "⏱️ Persistence", "value": f"{bias.persistence_snapshots} evaluations ({bias.persistence_duration_seconds:.0f}s duration)", "inline": True},
        {"name": "💡 Trader Guidance", "value": f"Awaiting pullback re-test near **{bias.wall_strike:.0f}**. Do NOT chase breakdown/bounce.", "inline": False},
    ]

    payload = {
        "embeds": [
            {
                "title": header,
                "color": 16776960,  # Yellow / Amber
                "fields": fields,
                "footer": {"text": "ARES Trading System • Watchlist Observation"}
            }
        ]
    }

    return payload


async def send_watchlist_alert(bias: OIWallBias, spot: float) -> bool:
    """Direct fallback: deliver the same watch with an unavailable Jev field."""
    if not settings.oi_wall_enable_watchlist_alert or not settings.discord_webhook_url:
        return False
    payload = build_watchlist_payload(bias, spot)
    if settings.oi_watch_jev_enabled:
        minimum = settings.oi_watch_jev_minimum_move_points
        threshold = spot - minimum if bias.direction.value == "BEARISH" else spot + minimum
        comparison = "below" if bias.direction.value == "BEARISH" else "above"
        payload["embeds"][0]["fields"].append({
            "name": "🧠 JEV Prediction",
            "value": f"Chance of closing {comparison} {threshold:.2f}: unavailable\n"
                     f"{minimum:g}+ points beyond watch price.\n"
                     "Outlook: Unavailable\nSupporting evidence: Assessment unavailable\n"
                     f"Assessment time: {payload['embeds'][0]['fields'][0]['value']}\n"
                     "Experimental model estimate.",
            "inline": False,
        })
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.post(settings.discord_webhook_url, json=payload)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"[-] Alerts: Failed to send watchlist alert: {e}")
            return False

def build_cancellation_payload(event: OIWallWatchCancellation) -> dict:
    """Freeze the existing watch cancellation format for either delivery path."""
    ist = timezone(timedelta(hours=5, minutes=30))

    def event_time(value):
        # Naive candle times follow the project's local-market-time convention.
        value = value.replace(tzinfo=ist) if value.tzinfo is None else value.astimezone(ist)
        return value.strftime("%d-%b-%Y %H:%M:%S IST")

    guidance = "No entry from this watch. Await a fresh setup watch and confirmed signal. This cancels the watch, not an existing trade."
    if event.reason.startswith("Final signal"):
        guidance = "Final signal alert was not delivered. Check recorded signals and any existing trade. This cancels the watch and does not close a trade."

    payload = {"embeds": [{
        "title": f"🛑 #{event.bias.wall_key} OI WALL WATCH CANCELLED ({event.bias.direction.value})",
        "color": 15158332,
        "fields": [
            {"name": "Wall", "value": f"{event.bias.wall_strike:.0f} {event.bias.wall_option_type}", "inline": True},
            {"name": "Watch armed", "value": event_time(event.watch_timestamp), "inline": True},
            {"name": "Cancelled at", "value": event_time(event.timestamp), "inline": True},
            {"name": "Spot at cancellation", "value": f"{event.spot:.2f}", "inline": True},
            {"name": "Reason", "value": event.reason, "inline": False},
            {"name": "Trader Guidance", "value": guidance, "inline": False},
        ],
        "footer": {"text": f"ARES • Watch cancellation • {event.event_id}"},
    }]}
    return payload


async def send_watchlist_cancellation(event: OIWallWatchCancellation) -> bool:
    """Close a delivered watch even if new watch alerts have since been disabled."""
    if not settings.discord_webhook_url:
        return False
    payload = build_cancellation_payload(event)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(settings.discord_webhook_url, json=payload)
            response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[-] Alerts: Failed to send watch cancellation: {exc}")
        return False


async def dispatch_oi_wall_watch_alerts(entry_filter, spot: float, *, outbox=None, **market) -> None:
    """Deliver cancellations before fresh watches; acknowledge only accepted posts."""
    try:
        if outbox is not None:
            for event in entry_filter.pending_watch_lifecycle:
                payload = build_cancellation_payload(event.cancellation) if event.cancellation else None
                outbox.submit_transition(event.event_id, event.action, payload)
                entry_filter.acknowledge_watch_lifecycle(event)
            observation = entry_filter.latest_watch_observation
            if observation is not None and settings.oi_wall_enable_watchlist_alert and settings.discord_webhook_url:
                payload = build_watchlist_payload(observation.bias, observation.spot, observation.timestamp)
                restart = OIWallWatchCancellation(
                    bias=observation.bias, watch_timestamp=observation.timestamp,
                    timestamp=observation.timestamp, spot=observation.spot,
                    reason="Trading process restarted", event_id=observation.event_id,
                )
                restart_payload = build_cancellation_payload(restart)
                for field in restart_payload["embeds"][0]["fields"]:
                    if field["name"] == "Cancelled at":
                        field["value"] = "__WORKER_TIME__"
                outbox.submit_watch(observation, payload, restart_payload, **market)
                entry_filter.acknowledge_watch_outbox(observation.event_id)
            return
        # No durable owner: retain the legacy retry/cancellation behavior.
        for event in entry_filter.pending_watch_lifecycle:
            entry_filter.acknowledge_watch_lifecycle(event)
        for event in entry_filter.pending_watchlist_cancellations:
            if not await send_watchlist_cancellation(event):
                return
            entry_filter.acknowledge_watchlist_cancellation(event.event_id)
        watch = entry_filter.latest_watchlist_event
        if watch is not None and await send_watchlist_alert(watch, spot):
            entry_filter.acknowledge_watchlist(watch.wall_key)
    except Exception as exc:
        # Notification failures must not interrupt signal persistence or trading.
        print(f"[-] Alerts: OI watch delivery failed: {exc}")


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
    if update_type == "ATR_SL_UPDATED":
        return "ATR trailing stop tightened."
    if update_type == "ATR_SL_HIT":
        return "5-minute close crossed ATR Stop Loss. Runner exit triggered."
    if update_type == "T2_HIT":
        return "Target 2 Reached! Trade Closed with Full Profit."
    if update_type == "STOPPED_OUT_AT_BE":
        return "Trailing Stop Loss Hit at Entry. T1 Profit Locked; Trade Closed."
    if update_type == "SL_HIT":
        # Pre-TASK-198 rows can still reach here with a trailed stop; keep
        # reporting those as the break-even exit they were.
        if trade.get("state") == "T1_HIT" or trade.get("stop_loss") == trade.get("entry_price"):
            return "Trailing Stop Loss Hit at Entry. Trade Closed."
        return "Stop Loss Hit. Trade Closed."
    return f"Trade Closed ({update_type})."


def _option_lifecycle_estimate(trade, spot, update_type):
    """Project option lifecycle outcomes without mislabelling spot events as fills."""
    sizing = (trade.get("market_context") or {}).get("options_sizing") or {}
    if not sizing:
        return ""
    entry = sizing.get("entry_limit") or sizing.get("premium") or None
    lot_size = sizing.get("lot_size", settings.nifty_lot_size)
    t1 = sizing.get("target_1") or sizing.get("option_target") or None
    exit_price = None
    if update_type == "T1_HIT":
        exit_price = t1
    elif update_type == "T2_HIT":
        exit_price = sizing.get("target_2")
    elif update_type == "STOPPED_OUT_AT_BE":
        exit_price = entry
    else:
        greeks = sizing.get("greeks") or {}
        delta, gamma = greeks.get("delta"), greeks.get("gamma")
        values = (entry, delta, gamma, spot, trade.get("entry_price"))
        if all(_number(value) != "unavailable" for value in values):
            move = float(spot) - float(trade["entry_price"])
            exit_price = float(entry) + float(delta) * move + .5 * float(gamma) * move * move
            if exit_price <= 0:
                exit_price = None
    pnl_per_lot = None
    if all(_number(value) != "unavailable" for value in (entry, exit_price, lot_size)) and float(lot_size) > 0:
        pnl_per_lot = (float(exit_price) - float(entry)) * float(lot_size)
    if update_type == "ATR_SL_UPDATED":
        greeks = sizing.get("greeks") or {}
        values = (entry, greeks.get("delta"), greeks.get("gamma"), trade.get("stop_loss"), trade.get("entry_price"))
        option_sl = None
        if all(_number(value) != "unavailable" for value in values):
            move = float(trade["stop_loss"]) - float(trade["entry_price"])
            option_sl = float(entry) + float(greeks["delta"]) * move + .5 * float(greeks["gamma"]) * move * move
        return f"\n🛡️ **Estimated Option SL**: ₹ {_number(option_sl)} (model estimate)"
    text = (
        f"\n💰 **Estimated Option Exit**: ₹ {_number(exit_price)} "
        f"(₹ {_number(pnl_per_lot, '+.2f')} / lot)\n"
        "Option figures are model estimates, not confirmed fills."
    )
    if update_type == "T1_HIT":
        text += f"\n🛡️ **SL Status**: Break-Even Spot {_number(trade['entry_price'])}; entry premium reference ₹ {_number(entry)}. 50% partial-booking model."
    if update_type == "STOPPED_OUT_AT_BE":
        lots = sizing.get("final_lots", sizing.get("lots", sizing.get("suggested_lots")))
        if all(_number(value) != "unavailable" for value in (t1, entry, lots, lot_size)) and float(lots) > 0 and float(lot_size) > 0:
            net = (float(t1) - float(entry)) * float(lot_size) * float(lots) * .5
            text += f"\n💰 **Estimated Trade PnL**: ₹ {_number(net, '+.2f')} (50% T1 booking model). Spot break-even does not guarantee option break-even."
    return text


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
    if update_type in {"ATR_SL_UPDATED", "ATR_SL_HIT"}:
        atr = (trade.get("market_context") or {}).get("atr_trailing") or {}
        description += f"\n🛑 **Previous SL**: {_number(atr.get('previous_sl', trade['stop_loss']))}\n📐 **Method**: 5-minute ATR trailing"
    description += _option_lifecycle_estimate(trade, spot, update_type)

    payload = {
        "embeds": [
            {
                "title": f"{icon} #{trade.get('display_id') or trade.get('signal_id', '0000')} TRADE UPDATE: {trade['setup_type']} ({trade['direction']})",
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
