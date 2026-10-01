"""Delivered watch lifecycle and cancellation delivery regressions."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from detectors.oi_wall_entry import OIWallEntryFilter
from models import OHLCVCandle, OIWallBias, Direction
from config import settings
from config_profiles import NON_EXPIRY_CONFIG


@pytest.fixture(autouse=True)
def normal_profile():
    previous = settings._tuning
    settings.apply_profile(NON_EXPIRY_CONFIG)
    yield
    settings.apply_profile(previous)


def wall_bias(strike, side, timestamp, persistence):
    return OIWallBias(
        wall_key=f"{side}:{int(strike)}",
        wall_strike=strike,
        wall_option_type=side,
        direction=Direction.BULLISH if side == "PE" else Direction.BEARISH,
        trade_option_type="CE" if side == "PE" else "PE",
        wall_oi=6_299_085,
        wall_oi_change_pct=9.94,
        relative_percentile=90.0,
        first_seen=timestamp,
        last_seen=timestamp,
        persistence_snapshots=persistence,
        persistence_duration_seconds=0.0,
        state="TRACKING",
        initial_interaction_timestamp=None,
        initial_interaction_price=None,
        favourable_excursion_pts=0.0,
        reasons=("OI wall retest",),
    )


def update(entry_filter, side, strike, timestamp, persistence, prices):
    candle = OHLCVCandle(timestamp=timestamp, open=prices[0], high=prices[1], low=prices[2], close=prices[3], volume=1000)
    return entry_filter.update(wall_bias(strike, side, timestamp, persistence), candle, [])

T0 = datetime(2026, 10, 1, 9, 17)


def candle(minute=2, close=24055):
    return OHLCVCandle(timestamp=T0 + timedelta(minutes=minute), open=24070,
                      high=max(24075, close), low=24050, close=close, volume=1000)


def ready(delivered=True):
    f = OIWallEntryFilter()
    update(f, "CE", 24100, T0, 2, (24085, 24085, 24065, 24075))
    update(f, "CE", 24100, T0 + timedelta(minutes=1), 3, (24070, 24072, 24050, 24055))
    assert f.latest_watchlist_event is not None
    if delivered:
        f.acknowledge_watchlist("CE:24100")
    return f


@pytest.mark.parametrize("reason", ["disappearance", "breach", "replacement", "rr"])
def test_delivered_watch_cancellation_for_each_expiry(reason):
    f = ready()
    if reason == "disappearance":
        f.update(None, candle(), [])
    elif reason == "breach":
        f.update(wall_bias(24100, "CE", T0, 4), candle(close=24101), [])
    elif reason == "replacement":
        f.update(wall_bias(24200, "CE", T0, 4), candle(), [])
    else:
        decision = update(f, "CE", 24100, T0 + timedelta(minutes=2), 4, (24085, 24088, 24070, 24075))
        f.acknowledge(decision, "REJECTED_BY_RR")
    event = f.pending_watchlist_cancellations[0]
    assert event.bias.wall_key == "CE:24100"
    assert event.reason
    assert event.watch_timestamp == T0 + timedelta(minutes=1)
    f.update(None, candle(3), [])
    f.update(None, candle(4), [])
    assert f.pending_watchlist_cancellations == (event,)
    f.acknowledge_watchlist_cancellation("unrelated")
    assert f.pending_watchlist_cancellations == (event,)
    f.acknowledge_watchlist_cancellation(event.event_id)
    f.update(None, candle(5), [])
    assert f.pending_watchlist_cancellations == ()


def test_unsent_watch_and_emitted_trade_never_cancel():
    f = ready(delivered=False)
    f.update(None, candle(), [])
    assert not f.pending_watchlist_cancellations
    f = ready()
    decision = update(f, "CE", 24100, T0 + timedelta(minutes=2), 4, (24085, 24088, 24070, 24075))
    f.acknowledge(decision, "EMITTED")
    f.update(None, candle(3), [])
    assert not f.pending_watchlist_cancellations


def test_same_strike_rearms_without_losing_old_cancellation():
    f = ready()
    f.update(None, candle(), [])
    old = f.pending_watchlist_cancellations[0]
    update(f, "CE", 24100, T0 + timedelta(minutes=3), 2, (24085, 24085, 24065, 24075))
    update(f, "CE", 24100, T0 + timedelta(minutes=4), 3, (24070, 24072, 24050, 24055))
    assert f.latest_watchlist_event is not None
    f.acknowledge_watchlist("CE:24100")
    f.update(None, candle(5), [])
    new = f.pending_watchlist_cancellations[1]
    assert old.event_id != new.event_id
    f.acknowledge_watchlist_cancellation(old.event_id)
    assert f.pending_watchlist_cancellations == (new,)


def test_session_reset_expires_delivered_watch_preserving_outbox():
    f = ready()
    f.reset_session(candle(24 * 60))
    assert f.state == "NO_WALL"
    event = f.pending_watchlist_cancellations[0]
    assert event.reason == "Trading session changed"
    f.reset_session(candle(2 * 24 * 60))
    assert f.pending_watchlist_cancellations == (event,)


@pytest.mark.asyncio
async def test_dispatch_retries_failed_cancel_and_dedupes_success():
    from alerts import dispatch_oi_wall_watch_alerts
    f = ready()
    f.update(None, candle(), [])
    with patch("alerts.send_watchlist_cancellation", new_callable=AsyncMock) as send:
        send.side_effect = [False, RuntimeError("offline"), True]
        await dispatch_oi_wall_watch_alerts(f, 24055)
        f.update(None, candle(3), [])
        await dispatch_oi_wall_watch_alerts(f, 24045)
        assert len(f.pending_watchlist_cancellations) == 1
        await dispatch_oi_wall_watch_alerts(f, 24040)
        await dispatch_oi_wall_watch_alerts(f, 24035)
        assert send.await_count == 3
        assert not f.pending_watchlist_cancellations


@pytest.mark.asyncio
async def test_cancellation_webhook_payload_and_failures():
    from alerts import send_watchlist_cancellation
    f = ready()
    f.update(None, candle(), [])
    event = f.pending_watchlist_cancellations[0]
    with patch("alerts.settings") as settings, patch("alerts.httpx.AsyncClient") as client_cls:
        settings.discord_webhook_url = "https://discord.invalid/test"
        settings.oi_wall_enable_watchlist_alert = False  # Already delivered watch still needs closing.
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock()
        client.post.return_value.raise_for_status = lambda: None
        assert await send_watchlist_cancellation(event)
        embed = client.post.call_args.kwargs["json"]["embeds"][0]
        assert "CANCELLED" in embed["title"]
        assert "CE:24100" in embed["title"]
        values = " ".join(field["value"] for field in embed["fields"])
        assert "24055.00" in values and event.reason in values
        assert "No entry" in values
        client.post.side_effect = RuntimeError("offline")
        assert not await send_watchlist_cancellation(event)
        settings.discord_webhook_url = ""
        assert not await send_watchlist_cancellation(event)


@pytest.mark.asyncio
async def test_failed_cancel_precedes_fresh_same_strike_watch():
    from alerts import dispatch_oi_wall_watch_alerts
    f = ready()
    f.update(None, candle(), [])
    update(f, "CE", 24100, T0 + timedelta(minutes=3), 2, (24085, 24085, 24065, 24075))
    update(f, "CE", 24100, T0 + timedelta(minutes=4), 3, (24070, 24072, 24050, 24055))
    delivered = []

    async def cancel(event):
        delivered.append("cancel")
        return True

    async def watch(bias, spot):
        delivered.append("watch")
        return True

    with patch("alerts.send_watchlist_cancellation", new_callable=AsyncMock, return_value=False), \
         patch("alerts.send_watchlist_alert", new_callable=AsyncMock) as send_watch:
        await dispatch_oi_wall_watch_alerts(f, 24055)
        send_watch.assert_not_awaited()
        assert f.latest_watchlist_event is not None
    with patch("alerts.send_watchlist_cancellation", side_effect=cancel), \
         patch("alerts.send_watchlist_alert", side_effect=watch):
        await dispatch_oi_wall_watch_alerts(f, 24055)
    assert delivered == ["cancel", "watch"]
    assert not f.pending_watchlist_cancellations
    assert f.latest_watchlist_event is None


@pytest.mark.parametrize("outcome", ["SUPPRESSED_BY_COOLDOWN", "SUPPRESSED_BY_PRIORITY"])
def test_suppressed_retest_keeps_delivered_watch_active(outcome):
    f = ready()
    decision = update(f, "CE", 24100, T0 + timedelta(minutes=2), 4, (24085, 24088, 24070, 24075))
    f.acknowledge(decision, outcome)
    assert not f.pending_watchlist_cancellations
    f.update(None, candle(3), [])
    assert len(f.pending_watchlist_cancellations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted,result", [(False, None), (True, False), (True, RuntimeError("offline")), (True, True)])
async def test_final_signal_delivery_closes_watch_only_after_confirmation(persisted, result):
    from main import _deliver_signal_alert
    from models import SetupType
    from unittest.mock import MagicMock
    f = ready()
    decision = update(f, "CE", 24100, T0 + timedelta(minutes=2), 4, (24085, 24088, 24070, 24075))
    f.acknowledge(decision, "EMITTED")
    signal = MagicMock(setup_type=SetupType.OI_WALL_REJECTION, oi_wall_context={"wall_key": "CE:24100"})
    with patch("main.send_discord", new_callable=AsyncMock) as send, \
         patch("alerts.send_watchlist_cancellation", new_callable=AsyncMock, return_value=False):
        if isinstance(result, Exception):
            send.side_effect = result
        else:
            send.return_value = result
        await _deliver_signal_alert(signal, 24075, persisted, f, candle(close=24075))
        if not persisted:
            send.assert_not_awaited()
    if result is True:
        f.reset_session(candle(24 * 60))
        assert not f.pending_watchlist_cancellations
    else:
        event = f.pending_watchlist_cancellations[0]
        assert "persistence" in event.reason if not persisted else "Discord" in event.reason
        assert f.state == "CONSUMED"
        f.update(None, candle(3), [])
        assert f.pending_watchlist_cancellations == (event,)


@pytest.mark.asyncio
async def test_failed_final_alert_cancellation_does_not_claim_no_trade_entry():
    from alerts import send_watchlist_cancellation
    f = ready()
    decision = update(f, "CE", 24100, T0 + timedelta(minutes=2), 4, (24085, 24088, 24070, 24075))
    f.acknowledge(decision, "EMITTED")
    f.acknowledge_signal_alert("CE:24100", False, candle(), "Final signal Discord delivery failed; a trade may already be tracked")
    with patch("alerts.settings") as config, patch("alerts.httpx.AsyncClient") as client_cls:
        config.discord_webhook_url = "https://discord.invalid/test"
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock()
        client.post.return_value.raise_for_status = lambda: None
        assert await send_watchlist_cancellation(f.pending_watchlist_cancellations[0])
        fields = client.post.call_args.kwargs["json"]["embeds"][0]["fields"]
        guidance = next(field["value"] for field in fields if field["name"] == "Trader Guidance")
        assert "No entry" not in guidance
        assert "existing trade" in guidance
