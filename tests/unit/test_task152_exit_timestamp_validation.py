import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock
import asyncio

from position_manager import PositionManager
from storage import AnalyticsLogger
from reports import fetch_closed_trades


@pytest.fixture
def mock_supabase():
    mock = MagicMock()
    
    analytics_table = MagicMock()
    analytics_table.select().eq().execute.return_value = MagicMock(data=[
        {"entry_price": 100, "direction": "BULLISH", "signal_id": "1", "entry_timestamp": "2026-09-01T10:00:00+00:00"}
    ])
    
    ml_table = MagicMock()
    
    def _table(name):
        if name == "trade_analytics":
            return analytics_table
        if name == "ml_collection":
            return ml_table
        return MagicMock()
        
    mock.table.side_effect = _table
    mock.analytics_table = analytics_table
    mock.ml_table = ml_table
    return mock


@pytest.fixture
def analytics_logger(mock_supabase):
    with patch("storage.create_client", return_value=mock_supabase):
        from storage import AnalyticsLogger
        logger = AnalyticsLogger()
        logger.supabase = mock_supabase
        return logger


def test_log_exit_requires_and_validates_timestamp(analytics_logger, mock_supabase):
    analytics_logger.log_exit("trade-1", 110, "T1_HIT", exit_timestamp="2026-09-01T10:05:00+00:00")
    
    mock_supabase.analytics_table.update.assert_called_once()
    update_data = mock_supabase.analytics_table.update.call_args[0][0]
    assert "exit_timestamp" in update_data
    assert update_data["exit_timestamp"] == "2026-09-01T10:05:00+00:00"


def test_log_exit_flags_inverted_timestamp(analytics_logger, mock_supabase):
    # entry is 10:00, exit is 09:55
    analytics_logger.log_exit("trade-1", 110, "T1_HIT", exit_timestamp="2026-09-01T09:55:00+00:00")
    
    update_data = mock_supabase.analytics_table.update.call_args[0][0]
    assert update_data.get("time_metrics_excluded") is True


def test_log_exit_retry_mechanism(analytics_logger):
    mock_supa = MagicMock()
    analytics_table = MagicMock()
    # Fail first 2 times
    analytics_table.select().eq().execute.side_effect = [
        MagicMock(data=[]),
        MagicMock(data=[]),
        MagicMock(data=[{"entry_price": 100, "direction": "BULLISH", "signal_id": "1", "entry_timestamp": "2026-09-01T10:00:00+00:00"}])
    ]
    def _table(name):
        if name == "trade_analytics":
            return analytics_table
        return MagicMock()
    
    mock_supa.table.side_effect = _table
    analytics_logger.supabase = mock_supa
    analytics_logger.log_exit("trade-1", 110, "T1_HIT", exit_timestamp="2026-09-01T10:05:00+00:00")
    
    assert analytics_table.select().eq().execute.call_count == 3
    analytics_table.update.assert_called_once()


@pytest.mark.asyncio
async def test_position_manager_passes_candle_timestamp():
    with patch("position_manager.create_client"):
        pm = PositionManager()
    
    pm.is_initialized = True
    pm.active_trades = [{
        "id": "trade-1",
        "state": "OPEN",
        "direction": "BULLISH",
        "setup_type": "OI_WALL_REJECTION",
        "entry_price": 100.0,
        "entry_timestamp": "2026-09-01T10:00:00+00:00",
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0
    }]
    
    mock_analytics = MagicMock()
    pm.analytics = mock_analytics
    
    candle_ts = datetime(2026, 9, 1, 10, 5, 0, tzinfo=timezone.utc)
    
    with patch("position_manager.send_trade_update") as mock_send_update:
        events = await pm.update_trades(125.0, candle_high=125.0, candle_low=120.0, candle_timestamp=candle_ts)
        assert len(events) == 1
        assert events[0][1] == "T2_HIT"
        
        # Check that write is queued
        assert "trade-1" in pm._trade_writes
        await pm._trade_writes["trade-1"]
        await asyncio.sleep(0.05)
        
        # Analytics log_exit should have been called with exit_ts
        mock_analytics.log_exit.assert_called_once()
        args, kwargs = mock_analytics.log_exit.call_args
        assert kwargs.get("exit_timestamp") == "2026-09-01T10:05:00+00:00"


def test_reports_excludes_flagged_records():
    mock_supabase = MagicMock()
    # Mock data returned by DB
    mock_supabase.table().select().eq().gte().lte().execute.return_value = MagicMock(data=[
        {"pnl_points": 10, "entry_timestamp": "2026-09-01T10:00:00+00:00", "exit_timestamp": "2026-09-01T10:05:00+00:00"},
        {"pnl_points": 20, "entry_timestamp": "2026-09-01T10:00:00+00:00", "exit_timestamp": "2026-09-01T09:55:00+00:00"}
    ])
    
    trades = fetch_closed_trades(mock_supabase, "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00")
    
    assert len(trades) == 1
    assert trades[0]["pnl_points"] == 10
