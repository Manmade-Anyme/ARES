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

    @pytest.mark.asyncio
    async def test_sleep_with_tick_exits_persists_receipt_only_quote_before_flush(self, monkeypatch, mock_supabase):
        """Receipt ticks update the persisted quote with receipt time before the buffered continue."""
        import main
        from unittest.mock import AsyncMock

        quote_service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: quote_service)

        from datetime import timedelta
        received_at = datetime.now(timezone.utc).replace(microsecond=0)
        exchange_at = received_at - timedelta(milliseconds=1)
        feed = MagicMock()
        feed.is_active = True
        feed.get_observations_since.return_value = [
            {
                "price": 23349.0,
                "exchange_at": exchange_at,
                "received_at": received_at,
                "source": "dhan_websocket_tick",
            },
            {
                "price": 23350.0,
                "exchange_at": None,
                "received_at": received_at,
                "source": "dhan_websocket_receipt",
            },
        ]
        pm = PositionManager.__new__(PositionManager)
        pm.active_trades = []
        pm._reference_price_evidence = {}
        pm.resolve_reference_ambiguities_from_feed = AsyncMock(return_value=0)

        monkeypatch.setattr("main.asyncio.sleep", AsyncMock())
        monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1.0)

        await main._sleep_with_tick_exits(1.0, feed, pm, MagicMock())

        persisted = mock_supabase.table.return_value.upsert.call_args.args[0]
        assert persisted["price"] == 23350.0
        assert persisted["source"] == "dhan_websocket_receipt"
        assert datetime.fromisoformat(persisted["observed_at"].replace("Z", "+00:00")) == received_at
        evidence = pm._reference_price_evidence["spot_quotes"]
        assert evidence[0]["price"] == 23349.0
        assert datetime.fromisoformat(evidence[0]["observed_at"]) == exchange_at
        assert evidence[1]["price"] == 23350.0
        assert datetime.fromisoformat(evidence[1]["observed_at"]) == received_at

    @pytest.mark.asyncio
    async def test_rest_fallback_quotes_selected_contract_when_nearest_chain_fails(self, monkeypatch):
        import main
        from unittest.mock import AsyncMock

        selected = {
            "expiry": "2026-11-12", "strike": 23400.0,
            "option_type": "PE", "security_id": "53941",
        }
        observation = {
            "price": 151.25, "observed_at": "2026-11-03T04:55:01+00:00",
            "source": "dhan_last_trade",
        }
        price_fetcher = MagicMock()
        price_fetcher.fetch_latest_spot = AsyncMock(return_value=23350.0)
        price_fetcher.fetch_option_observation = AsyncMock(return_value=observation)
        oi_fetcher = MagicMock()
        oi_fetcher.get_nearest_expiry = AsyncMock(return_value="2026-11-05")
        oi_fetcher.fetch_chain = AsyncMock(side_effect=ValueError("chain unavailable"))
        position_manager = MagicMock()
        position_manager.active_trades = []
        position_manager.get_selected_option_contract.return_value = selected
        position_manager.update_trades = AsyncMock(return_value=[])
        dispatcher = MagicMock()
        dispatcher.on_spot = AsyncMock()
        dispatcher.on_snapshot = AsyncMock()
        quote_service = MagicMock()
        quote_service.flush = AsyncMock(return_value=True)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: quote_service)
        monkeypatch.setattr(main, "_session_now", lambda: datetime(2026, 11, 3, 10, 0))

        with pytest.raises(ValueError, match="chain unavailable"):
            await main._monitor_rest_without_candle(
                price_fetcher, oi_fetcher, position_manager, MagicMock(), dispatcher
            )

        price_fetcher.fetch_option_observation.assert_awaited_once_with({
            **selected, "exchange_segment": "NSE_FNO",
        })
        quote_service.record_option_quote.assert_called_once_with(
            selected, 151.25, observation["observed_at"], source="dhan_last_trade"
        )
        assert quote_service.flush.await_count == 2

    @pytest.mark.asyncio
    async def test_sleep_with_tick_exits_stale_feed_does_not_refresh_timestamp(self, monkeypatch):
        """Verifies that a stalled/disconnected feed does not publish old prices with current time."""
        import main
        from unittest.mock import AsyncMock

        mock_qs = MagicMock()
        mock_qs.flush = AsyncMock(return_value=True)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: mock_qs)

        feed = MagicMock()
        feed.is_active = True
        # No buffered ticks
        feed.get_observations_since.return_value = []
        # Stale observation (> 5s)
        feed.get_latest_observation.return_value = None
        # Retains old cached price
        feed.get_latest_price.return_value = 23350.0

        pm = MagicMock()
        pm.active_trades = []
        engine = MagicMock()

        monkeypatch.setattr("main.asyncio.sleep", AsyncMock())
        monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1.0)

        await main._sleep_with_tick_exits(1.0, feed, pm, engine)
        # Stale price must NOT be recorded with refreshed timestamp
        mock_qs.record_spot_quote.assert_not_called()

    def test_position_manager_init_does_not_pass_anon_client_to_quote_service(self, monkeypatch):
        """Verifies PositionManager initializes quote service via service-role helper without passing its anon client."""
        import quote_service
        quote_service._global_quote_service = None

        mock_get_quote_service = MagicMock()
        monkeypatch.setattr("quote_service.get_quote_service", mock_get_quote_service)
        monkeypatch.setattr("position_manager.create_client", MagicMock())
        monkeypatch.setattr("position_manager.PositionManager._initialize_db", MagicMock())

        from position_manager import PositionManager
        pm = PositionManager()
        # Must be called with no arguments so quote_service resolves the service-role client
        mock_get_quote_service.assert_called_with()

    @pytest.mark.asyncio
    async def test_multiple_trades_closure_reconciles_and_clears_old_option_quote(self, monkeypatch):
        """Verifies when Trade 1 closes, its cached quote is cleared if Trade 2 has a different contract."""
        import quote_service
        from position_manager import PositionManager

        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.upsert.return_value = mock_table
        mock_table.delete.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=[])

        qs = quote_service.QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        quote_service._global_quote_service = qs

        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c2 = {"expiry": "2026-11-12", "strike": 23400, "option_type": "PE", "security_id": "53941"}

        # Seed quote service with Trade 1's contract
        qs.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")
        qs.record_spot_quote(23310.0, "2026-11-02T05:00:00Z")
        assert qs.get_cached_option_contract()["security_id"] == "53940"

        # PositionManager with Trade 1 (CLOSED) and Trade 2 (ACTIVE with contract c2)
        pm = MagicMock(spec=PositionManager)
        pm.active_trades = [
            {"id": "trade-1", "state": "CLOSED", "market_context": {"options_sizing": c1}},
            {"id": "trade-2", "state": "ACTIVE", "market_context": {"options_sizing": c2}},
        ]
        # Bind real get_selected_option_contract and _cleanup_option_selection_if_terminal
        pm.get_selected_option_contract = PositionManager.get_selected_option_contract.__get__(pm, PositionManager)
        pm._cleanup_option_selection_if_terminal = PositionManager._cleanup_option_selection_if_terminal.__get__(pm, PositionManager)

        # Trade 1 closes -> triggers cleanup
        pm._cleanup_option_selection_if_terminal()

        # Cached quote for contract 1 MUST be cleared because newly selected contract 2 differs
        assert qs.get_cached_option_contract() is None
        assert qs._clear_option_pending is True

        # Now spot quote arrives and flushes
        qs.record_spot_quote(23315.0, "2026-11-02T05:01:00Z")
        await qs.flush(force=True)

        # Deletion for OPTION:SELECTED must have been executed, and c1 must NOT have been upserted
        mock_table.delete.assert_called_once()
        mock_table.eq.assert_called_with("instrument_id", "OPTION:SELECTED")

        # Verify upsert calls: only UNDERLYING was upserted, never OPTION:SELECTED
        for call in mock_table.upsert.call_args_list:
            upserted_row = call[0][0]
            assert upserted_row["instrument_id"] == "UNDERLYING:NIFTY"
            assert upserted_row["instrument_id"] != "OPTION:SELECTED"

    def test_multiple_trades_closure_retains_quote_if_same_contract(self):
        """Verifies when Trade 1 closes, its cached quote is retained if Trade 2 uses the exact same contract."""
        import quote_service
        from position_manager import PositionManager

        qs = quote_service.QuotePersistenceService(supabase_client=MagicMock(), throttle_seconds=0.0)
        quote_service._global_quote_service = qs

        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c1_dup = {"expiry": "2026-11-05", "strike": 23300.0, "option_type": "ce", "security_id": "53940"}

        qs.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")

        pm = MagicMock(spec=PositionManager)
        pm.active_trades = [
            {"id": "trade-1", "state": "CLOSED", "market_context": {"options_sizing": c1}},
            {"id": "trade-2", "state": "ACTIVE", "market_context": {"options_sizing": c1_dup}},
        ]
        pm.get_selected_option_contract = PositionManager.get_selected_option_contract.__get__(pm, PositionManager)
        pm._cleanup_option_selection_if_terminal = PositionManager._cleanup_option_selection_if_terminal.__get__(pm, PositionManager)

        pm._cleanup_option_selection_if_terminal()

        # Must retain cached quote
        assert qs.get_cached_option_contract() is not None
        assert qs.get_cached_option_contract()["security_id"] == "53940"
        assert qs._clear_option_pending is False

