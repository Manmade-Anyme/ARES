"""ATR runner outcomes retain the fact that T1 was reached."""
import pytest

from ml_signal.labeling import classify_ares_outcome, label_from_ares_outcome
from ml_signal.backfill_labels import repair_stuck_open_trades
from tests.unit.test_task194_ml_labels_and_oi_distribution import _FakeSupabase


@pytest.mark.parametrize('t1_is_win', [True, False])
def test_atr_exit_is_retained_in_labeled_trade_data(t1_is_win):
    record = {'result_state': 'ATR_SL_HIT', 'entry_timestamp': '2026-10-21T05:00:00Z',
              'entry_price': 24000, 'direction': 'BULLISH', 'setup_type': 'TREND_CONTINUATION'}
    assert classify_ares_outcome('ATR_SL_HIT', t1_is_win) == int(t1_is_win)
    labeled = label_from_ares_outcome([record], t1_is_win)
    assert len(labeled) == 1
    assert labeled.iloc[0]['outcome'] == 'ATR_SL_HIT'
    assert labeled.iloc[0]['label'] == int(t1_is_win)


def test_restart_repairs_atr_exit_and_keeps_partial_profit_model():
    rows = {
        'active_trades': [{'id': 'atr', 'state': 'CLOSED', 'entry_price': 24000,
                           'direction': 'BULLISH', 'exit_price': 24014,
                           'exit_timestamp': '2026-10-21T05:00:00Z',
                           'exit_type': 'ATR_SL_HIT', 'pnl_points_override': 17}],
        'trade_analytics': [{'id': 'atr', 'result_state': 'OPEN', 'entry_price': 24000,
                             'direction': 'BULLISH'}],
        'ml_collection': [{'id': 1, 'trade_id': 'atr'}],
    }
    repair_stuck_open_trades(_FakeSupabase(rows), apply=True)
    trade = rows['trade_analytics'][0]
    assert (trade['result_state'], trade['score'], trade['pnl_points']) == ('ATR_SL_HIT', 1, 17)
