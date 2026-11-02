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
