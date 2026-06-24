import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

from .config import MLConfig


def ist_now() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d-%b-%Y %H:%M:%S")


def format_prediction_alert(
    probability: float,
    confidence_tier: str,
    spot: float,
    source: str,
    signal_id: Optional[str] = None,
    signal_setup_type: Optional[str] = None,
    model_version: str = "v1",
) -> str:
    marker = "+" if probability >= 0.70 else "-"
    tier_emoji = "🟢" if confidence_tier == "HIGH" else "🟡" if confidence_tier == "MEDIUM" else "🔴"
    source_label = "ML CONTINUOUS SCAN" if source == "continuous" else "ML SIGNAL VERIFICATION"
    signal_ref = f"   📡 Signal   : #{signal_id} ({signal_setup_type})\n" if signal_id else ""

    msg = f"""```diff
{marker} 🧠 {tier_emoji} {source_label}
   
   🕒 Time     : {ist_now()} IST
   📍 Spot     : {spot:.2f}
   🎯 Prob(T1) : {probability:.1%}
   ⭐ Conf.    : {confidence_tier}
   🤖 Model    : {model_version}
{signal_ref}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```"""
    return msg


def format_prediction_summary(
    total_predictions: int,
    high_count: int,
    med_count: int,
    low_count: int,
    avg_probability: float,
    model_version: str,
    interval_minutes: int,
) -> str:
    msg = f"""```diff
+ 📊 ML PREDICTION SUMMARY ({interval_minutes}-min)
   
   🕒 Period  : Last {interval_minutes} minutes
   📊 Total   : {total_predictions} predictions
   🟢 HIGH    : {high_count}
   🟡 MEDIUM  : {med_count}
   🔴 LOW     : {low_count}
   📈 Avg Prob: {avg_probability:.1%}
   🤖 Model   : {model_version}
   ─────────────────────────────────────────────
```"""
    return msg


async def send_discord(
    webhook_url: str,
    content: str,
) -> None:
    if not webhook_url:
        return

    payload = {"content": content}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"[-] Discord ML alert failed: {type(e).__name__} - {e}")


async def send_prediction_alert(
    config: MLConfig,
    probability: float,
    confidence_tier: str,
    spot: float,
    source: str,
    signal_id: Optional[str] = None,
    signal_setup_type: Optional[str] = None,
) -> None:
    if not config.discord_webhook_url:
        return
    if probability < config.discord_min_probability_for_alert:
        return

    content = format_prediction_alert(
        probability=probability,
        confidence_tier=confidence_tier,
        spot=spot,
        source=source,
        signal_id=signal_id,
        signal_setup_type=signal_setup_type,
        model_version=config.active_model_version,
    )
    await send_discord(config.discord_webhook_url, content)


async def send_summary_alert(
    config: MLConfig,
    total_predictions: int,
    high_count: int,
    med_count: int,
    low_count: int,
    avg_probability: float,
) -> None:
    if not config.discord_webhook_url:
        return

    content = format_prediction_summary(
        total_predictions=total_predictions,
        high_count=high_count,
        med_count=med_count,
        low_count=low_count,
        avg_probability=avg_probability,
        model_version=config.active_model_version,
        interval_minutes=config.discord_summary_interval_minutes,
    )
    await send_discord(config.discord_webhook_url, content)
