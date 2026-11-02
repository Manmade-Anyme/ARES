from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from position_manager import PositionManager
from quote_service import (
    format_frontend_quote_contract,
    get_quote_service,
    is_quote_stale,
    QuotePersistenceService,
    validate_option_contract,
)


@pytest.fixture
def mock_supabase():
    client = MagicMock()
    table_mock = MagicMock()
    client.table.return_value = table_mock
    table_mock.upsert.return_value.execute.return_value = MagicMock(data=[])
    table_mock.delete.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    table_mock.select.return_value.execute.return_value = MagicMock(data=[])
    return client


class TestPositionManagerQuoteIntegration:
    """Verifies PositionManager interaction with QuotePersistenceService."""

    def test_get_selected_option_contract_active_trade(self, mock_supabase):
        pm = PositionManager.__new__(PositionManager)
        pm.supabase = mock_supabase
        pm.active_trades = [
            {
                "id": "trade-1",
                "state": "OPEN",
                "market_context": {
                    "options_sizing": {
                        "expiry_date": "2026-11-03",
                        "strike": 23300,
                        "option_type": "CE",
                        "security_id": "53940",
                    }
                },
            }
        ]

        contract = pm.get_selected_option_contract()
        assert contract is not None
        assert contract["expiry"] == "2026-11-03"
        assert contract["strike"] == 23300.0
        assert contract["option_type"] == "CE"
        assert contract["security_id"] == "53940"

    def test_get_selected_option_contract_ignores_closed_trades(self, mock_supabase):
        pm = PositionManager.__new__(PositionManager)
        pm.supabase = mock_supabase
        pm.active_trades = [
            {
                "id": "trade-closed",
                "state": "CLOSED",
                "market_context": {
                    "options_sizing": {
                        "expiry_date": "2026-11-03",
                        "strike": 23300,
                        "option_type": "CE",
                        "security_id": "53940",
                    }
                },
            }
        ]
        assert pm.get_selected_option_contract() is None

    def test_get_selected_option_contract_fallback_to_reference(self, mock_supabase):
        pm = PositionManager.__new__(PositionManager)
        pm.supabase = mock_supabase
        pm.active_trades = [
            {
                "id": "trade-ref",
                "state": "T1_HIT",
                "market_context": {
                    "option_reference": {
                        "contract": {
                            "expiry_date": "2026-11-05",
                            "strike": 23400,
                            "option_type": "PE",
                            "security_id": "53945",
                        }
                    }
                },
            }
        ]
        contract = pm.get_selected_option_contract()
        assert contract is not None
        assert contract["expiry"] == "2026-11-05"
        assert contract["strike"] == 23400.0
        assert contract["option_type"] == "PE"
        assert contract["security_id"] == "53945"

    def test_record_reference_spot_quote_forwards_to_quote_service(self, mock_supabase):
        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        import quote_service
        quote_service._global_quote_service = service

        pm = PositionManager.__new__(PositionManager)
        pm.supabase = mock_supabase
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)
        result = pm.record_reference_spot_quote(23315.5, now.isoformat(), "dhan_websocket_tick")
        assert result is True

        payload = service.get_latest_quotes_payload()
        assert payload["underlying"]["currentSpot"] == 23315.5
        assert payload["underlying"]["symbol"] == "NIFTY"
        assert payload["underlying"]["updatedAt"] == "2026-11-02T04:52:00Z"

    def test_contract_separation_guarantees(self):
        """Verifies that live option LTP and spot never get confused with entry values."""
        entry_spot = 23200.0
        entry_premium = 80.0
        live_spot = 23310.0
        live_premium = 120.0

        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
            "price": live_premium,
            "observed_at": "2026-11-02T04:52:00Z",
        }
        underlying = {
            "symbol": "NIFTY",
            "price": live_spot,
            "observed_at": "2026-11-02T04:52:00Z",
        }

        frontend = format_frontend_quote_contract(underlying, contract)
        assert frontend["underlying"]["currentSpot"] == live_spot
        assert frontend["underlying"]["currentSpot"] != entry_spot
        assert frontend["selectedOption"]["currentPremium"] == live_premium
        assert frontend["selectedOption"]["currentPremium"] != entry_premium

    @pytest.mark.asyncio
    async def test_option_policy_terminal_cleanup(self, mock_supabase):
        """Verifies that terminal transitions for option_atr_v1 trades clear selected option quote."""
        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        import quote_service
        quote_service._global_quote_service = service

        # Populate a selected option quote
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        service.record_option_quote(contract, 125.5, "2026-11-02T04:52:00Z")
        assert service.get_latest_quotes_payload()["selectedOption"] is not None

        pm = PositionManager.__new__(PositionManager)
        pm.supabase = mock_supabase
        pm.active_trades = [
            {
                "id": "trade-opt-1",
                "state": "CLOSED",
                "stop_loss": 23200,
                "direction": "BULLISH",
                "entry_price": 23250,
                "market_context": {
                    "option_trailing": {
                        "policy_version": "option_atr_v1",
                        "phase": "CLOSED",
                        "contract": contract,
                    }
                },
            }
        ]

        # Call cleanup helper
        pm._cleanup_option_selection_if_terminal()
        assert service.get_latest_quotes_payload()["selectedOption"] is None

    def test_storage_get_supabase_client(self, monkeypatch):
        """Verifies storage.get_supabase_client constructs client using settings."""
        import storage
        monkeypatch.setattr(storage.settings, "supabase_url", "https://test.supabase.co")
        monkeypatch.setattr(storage.settings, "supabase_key", "test-anon-key")
        monkeypatch.setattr(storage.settings, "supabase_service_role_key", "test-service-key")

        mock_create = MagicMock()
        monkeypatch.setattr("storage.create_client", mock_create)

        client = storage.get_supabase_client()
        mock_create.assert_called_with("https://test.supabase.co", "test-service-key")

    @pytest.mark.asyncio
    async def test_sleep_with_tick_exits_flushes_buffered_ticks(self, monkeypatch):
        """Verifies that buffered WebSocket ticks trigger quote service flush."""
        import main
        from unittest.mock import AsyncMock

        mock_qs = MagicMock()
        mock_qs.flush = AsyncMock(return_value=True)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: mock_qs)

        feed = MagicMock()
        feed.is_active = True
        feed.get_observations_since.return_value = [
            {
                "price": 23350.0,
                "exchange_at": datetime.now(timezone.utc),
                "received_at": datetime.now(timezone.utc),
                "source": "dhan_websocket_tick",
            }
        ]
        pm = MagicMock()
        pm.active_trades = []
        engine = MagicMock()

        monkeypatch.setattr("main.asyncio.sleep", AsyncMock())
        monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1.0)

        await main._sleep_with_tick_exits(1.0, feed, pm, engine)
        assert mock_qs.flush.await_count >= 1
