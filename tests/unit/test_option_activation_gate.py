"""Enrollment never changes the already calculated entry/risk bracket."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from config import settings
from models import OptionTrailingContext
from position_manager import PositionManager
from tests.unit.test_manm176_review_persistence import MemoryDatabase, make_signal


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,approved,backend,ready,enrolled', [
    (False, '', True, True, False), (True, '', True, True, False),
    (True, 'unverified', True, True, False), (True, 'option_atr_v1', False, True, False),
    (True, 'option_atr_v1', True, False, False), (True, 'option_atr_v1', True, True, True),
])
async def test_actual_entry_requires_all_release_gates_and_preserves_prices(monkeypatch, enabled, approved, backend, ready, enrolled):
    database = MemoryDatabase()
    monkeypatch.setattr(settings, 'option_atr_enabled', enabled)
    monkeypatch.setattr(settings, 'option_atr_approved_version', approved)
    monkeypatch.setattr(settings, 'supabase_service_role_key', 'test-backend-only' if backend else '')
    signal = make_signal()
    signal.suggested_lots = signal.bracket.lots
    signal.option_premium = 99  # Reference quote intentionally differs from entry limit100.
    signal.option_sl = 95
    signal.market_context = {'tracking_mode':'SIGNAL_REFERENCE', 'unrelated': {'preserved':True}}
    original = deepcopy(signal.bracket)
    with patch('position_manager.create_client',return_value=database), patch('position_manager.AnalyticsLogger') as analytics:
        analytics.return_value.option_reference_accounting_ready.return_value = ready
        pm = PositionManager()
        await pm.add_trade(signal, 24000)
    trade = pm.active_trades[0]
    opt = trade['market_context'].get('option_trailing')
    assert bool(opt) is enrolled
    assert signal.bracket == original
    assert signal.suggested_lots == 2
    assert [trade[key] for key in ('entry_price','stop_loss','target_1','target_2')] == [24000,23990,24020,24040]
    assert trade['market_context']['unrelated'] == {'preserved':True}
    if enrolled:
        assert opt['contract']['security_id'] == '123'
        assert opt['contract']['expiry'] == '2099-01-01'
        assert opt['original_option_entry'] == 100
        assert opt['original_option_sl'] == 95
        assert opt['activated_at'] == trade['created_at']
        assert datetime.fromisoformat(opt['activated_at']) > datetime.fromisoformat(trade['entry_timestamp'])
        assert signal.market_context['option_trailing'] == opt


def test_restart_serialization_preserves_pending_source_and_exit_evidence():
    context = OptionTrailingContext.from_dict({
        'contract': {'security_id':'51333','exchange_segment':'NSE_FNO','expiry':'2026-10-27','strike':22550,'option_type':'PE'},
        'activated_at':'2026-10-23T04:38:38+00:00', 'indicator_history_from':'2026-10-13',
        'pending_index_observations':[{'event_at':'2026-10-23T05:30:00Z','price':22470}],
        'observed_index_guard_exit':{'status':'AWAITING_OPTION_BACKFILL'},
        'last_index_mark':{'event_at':'2026-10-23T05:25:00Z','price':22480},
        'source_candle':{'start_time':'2026-10-23T05:25:00Z','end_time':'2026-10-23T05:30:00Z'},
        'exit': {'reason':'ATR_SL_HIT','event_at':'2026-10-23T05:30:00Z','detected_at':'2026-10-23T05:30:02Z',
                 'option_close':150,'option_stop':152,'exited_fraction':.2,
                 'candle_start':'2026-10-23T05:25:00Z','candle_end':'2026-10-23T05:30:00Z'},
        'alert_events':{'id':{'status':'SENDING'}},
    })
    restored = OptionTrailingContext.from_dict(context.to_dict()).to_dict()
    assert restored == context.to_dict()
    assert restored['pending_index_observations'][0]['price'] == 22470
    assert restored['exit']['exited_fraction'] == .2
    assert restored['alert_events']['id']['status'] == 'SENDING'


def test_empty_typed_context_can_be_loaded_safely():
    assert OptionTrailingContext.from_dict(None).to_dict()['phase'] == 'PRE_T1'
