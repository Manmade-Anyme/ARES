"""Completed option-policy index stops retain the engine's re-entry behavior."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from engine import AresEngine
from tests.unit.test_option_lifecycle_commit_boundary import IST, make_pm


@pytest.mark.parametrize('path', ['tick', 'rest', 'fetch', 'final_fetch'])
@pytest.mark.asyncio
async def test_completed_option_initial_stop_releases_cooldown(monkeypatch, path):
    pm, trade, _ = make_pm(monkeypatch)
    candle = SimpleNamespace(timestamp=datetime(2026, 10, 23, 10, 9, tzinfo=IST),
                             open=22550, high=22580, low=22545, close=22560)
    fetcher = SimpleNamespace(watch_candles=[candle],
        fetch_option_candles=AsyncMock(return_value=[]),
        fetch_latest_candle=AsyncMock(return_value=candle),
        fetch_latest_spot=AsyncMock(return_value=22560))
    dispatcher = SimpleNamespace(on_candles=AsyncMock(), on_spot=AsyncMock(), on_snapshot=AsyncMock())
    engine = AresEngine.__new__(AresEngine)
    engine.last_signal_time = datetime.now()
    monkeypatch.setattr(main.settings, 'tick_exit_check_interval_seconds', 1)
    monkeypatch.setattr(main.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(main, '_session_now', lambda: datetime(2026, 10, 23, 10, 10))

    async def monitor():
        if path == 'tick':
            feed = SimpleNamespace(is_active=True, get_latest_price=lambda: None)
            await main._sleep_with_tick_exits(1, feed, pm, engine, dispatcher, price_fetcher=fetcher)
        elif path == 'rest':
            oi = SimpleNamespace(get_nearest_expiry=AsyncMock(return_value='2026-10-27'),
                                 fetch_chain=AsyncMock(return_value=(None, [])))
            await main._monitor_rest_without_candle(fetcher, oi, pm, engine, dispatcher)
        else:
            await main._fetch_candle_with_protection(fetcher, pm, dispatcher,
                engine=engine, finalizing=path == 'final_fetch')

    await monitor()
    assert trade['state'] == 'CLOSED'
    assert pm.analytics.rows[-1]['exit_reason'] == 'SL_HIT'
    assert engine.last_signal_time is None

    # Replaying the same history must not erase a subsequent signal's cooldown.
    new_signal_time = datetime.now()
    engine.last_signal_time = new_signal_time
    await monitor()
    assert engine.last_signal_time == new_signal_time


@pytest.mark.parametrize('event', ['T1_HIT', 'T2_HIT', 'STOPPED_OUT_AT_BE', 'ATR_SL_HIT', 'OPTION_SL_FLOOR_HIT', None])
@pytest.mark.asyncio
async def test_other_option_events_do_not_release_cooldown(event):
    contract = {'security_id': '51333'}
    positions = SimpleNamespace(active_trades=[{'state': 'T1_HIT', 'market_context': {
        'option_trailing': {'policy_version': 'option_atr_v1', 'contract': contract}}}],
        update_option_market_events=AsyncMock(return_value=[('trade-1950', event)] if event else []),
        update_atr_trailing=AsyncMock(return_value=[]))
    engine = AresEngine.__new__(AresEngine)
    signal_time = engine.last_signal_time = datetime.now()
    fetcher = SimpleNamespace(fetch_option_candles=AsyncMock(return_value=[]))
    await main._monitor_completed_atr([], positions, None, price_fetcher=fetcher, engine=engine)
    assert engine.last_signal_time == signal_time
