"""Provider warmup truncation must never reseed a live Wilder runner."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from atr_trailing import calculate_option_atr_bars
from models import OptionTrailingContext
from options_candles import OptionCandle
from tests.unit.test_option_conflict_recovery import post_t1
from tests.unit.test_option_lifecycle_commit_boundary import IST, make_pm


def history():
    start = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    return [OptionCandle(start+timedelta(minutes=5*i), start+timedelta(minutes=5*(i+1)),
                         200, 250 if i < 6 else 201, 150 if i < 6 else 199,
                         200, 1, '51333') for i in range(20)]


async def ingest(pm, bars, api):
    if api == 'timeline':
        return await pm.update_option_market_events([], {'51333': bars}, current_time=bars[-1].end)
    return await pm.update_option_atr_trailing({'51333': bars}, current_time=bars[-1].end)


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('api', ['direct', 'timeline'])
@pytest.mark.asyncio
async def test_truncated_seed_cannot_raise_stop_or_false_exit_and_backfill_resumes(monkeypatch, restart, api):
    pm, trade, _ = make_pm(monkeypatch)
    option = post_t1(trade)
    bars = history()
    await ingest(pm, bars, api)
    original_stop = option['locked_option_sl']
    assert original_stop == pytest.approx(185.22, abs=.01)
    assert calculate_option_atr_bars(bars[6:])[-1].raw_line == 196
    original_watermark = option['last_processed_option_bar_end']
    if restart:
        restored, restored_trade, _ = make_pm(monkeypatch)
        restored_trade.update(state=trade['state'], stop_loss=trade['stop_loss'])
        restored_trade['market_context']['option_trailing'] = OptionTrailingContext.from_dict(
            deepcopy(pm.analytics.rows[-1]['option_trailing'])).to_dict()
        restored.analytics = pm.analytics
        pm, trade = restored, restored_trade
        option = trade['market_context']['option_trailing']
    else:
        # Adopt the cached original seed for already loaded, unanchored context.
        option.pop('indicator_seed_start', None)
    end = bars[-1].end+timedelta(minutes=5)
    bars.append(OptionCandle(end-timedelta(minutes=5), end, 200, 201, 199, 200, 1, '51333'))
    await ingest(pm, bars[6:], api)
    assert option['locked_option_sl'] == original_stop
    assert option['last_processed_option_bar_end'] == original_watermark
    assert option['indicator_seed_start'] == bars[0].start.isoformat()
    assert trade['id'] not in pm._option_available_until
    if not restart:
        assert len(pm._option_history[trade['id']]) == 20
    end += timedelta(minutes=5)
    bars.append(OptionCandle(end-timedelta(minutes=5), end, 200, 201, 192, 193, 1, '51333'))
    await ingest(pm, bars[6:], api)
    assert trade['state'] == 'T1_HIT'
    assert option['locked_option_sl'] == original_stop
    assert option['last_processed_option_bar_end'] == original_watermark
    # Recover the exact original warmup; replay should match a continuous feed.
    await ingest(pm, bars, api)
    assert trade['state'] == 'T1_HIT'
    assert option['locked_option_sl'] == calculate_option_atr_bars(bars)[-1].raw_line
    assert original_stop < option['locked_option_sl'] < 193
    assert option['last_processed_option_bar_end'] == bars[-1].end.isoformat()
    assert option['indicator_seed_start'] == bars[0].start.isoformat()
    assert pm.analytics.rows[-1]['option_trailing']['indicator_seed_start'] == bars[0].start.isoformat()


@pytest.mark.asyncio
async def test_seed_pinned_before_t1_survives_serialization_and_ignores_earlier_prefix(monkeypatch):
    pm, trade, _ = make_pm(monkeypatch)
    bars = history()
    await ingest(pm, bars, 'direct')
    await pm.update_trades(22525, event_time=bars[-1].end+timedelta(minutes=1))
    durable = OptionTrailingContext.from_dict(pm.analytics.rows[-1]['option_trailing']).to_dict()
    assert durable['indicator_seed_start'] == bars[0].start.isoformat()
    restored, restored_trade, _ = make_pm(monkeypatch)
    restored_trade['market_context']['option_trailing'] = durable
    # A provider may later return more prior-session data than it initially did.
    earlier_end = datetime(2026, 10, 22, 15, 30, tzinfo=IST)
    earlier = OptionCandle(earlier_end-timedelta(minutes=5), earlier_end, 200, 400, 1, 200, 1, '51333')
    result = restored._prepare_option_history({'51333': [earlier]+bars}, bars[-1].end)
    assert result[trade['id']] == calculate_option_atr_bars(bars)
    assert durable['indicator_seed_start'] == bars[0].start.isoformat()
