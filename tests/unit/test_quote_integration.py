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
        await pm._quote_flush_task
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
        await pm._quote_flush_task

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
    @pytest.mark.parametrize("exchange_age_seconds", [45, None])
    async def test_sleep_with_tick_exits_uses_best_timestamp_for_fallback_quote(
        self, monkeypatch, mock_supabase, exchange_age_seconds
    ):
        """Preserve exchange time when present and use receipt time only without it."""
        import main
        from datetime import timedelta
        from unittest.mock import AsyncMock

        quote_service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: quote_service)
        received_at = datetime.now(timezone.utc)
        exchange_at = (
            received_at - timedelta(seconds=exchange_age_seconds)
            if exchange_age_seconds is not None else None
        )
        expected_at = exchange_at or received_at

        feed = MagicMock()
        feed.is_active = True
        feed.get_observations_since.return_value = []
        feed.get_latest_price.return_value = 23350.0
        feed.get_latest_observation.return_value = (23350.0, received_at, exchange_at)
        pm = PositionManager.__new__(PositionManager)
        pm.active_trades = []
        pm.resolve_reference_ambiguities_from_feed = AsyncMock(return_value=0)

        monkeypatch.setattr("main.asyncio.sleep", AsyncMock())
        monkeypatch.setattr(main.settings, "tick_exit_check_interval_seconds", 1.0)

        await main._sleep_with_tick_exits(1.0, feed, pm, MagicMock())
        await pm._quote_flush_task

        persisted = mock_supabase.table.return_value.upsert.call_args.args[0]
        assert datetime.fromisoformat(persisted["observed_at"].replace("Z", "+00:00")) == expected_at
        expected_source = "dhan_websocket_tick" if exchange_at else "dhan_websocket_receipt"
        assert persisted["source"] == expected_source
        assert is_quote_stale(
            persisted["observed_at"], reference_time=received_at
        ) is (exchange_at is not None)

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
        await position_manager._selected_option_quote_task
        await position_manager._quote_flush_task

        price_fetcher.fetch_option_observation.assert_awaited_once_with({
            **selected, "exchange_segment": "NSE_FNO",
        })
        quote_service.record_option_quote.assert_called_once_with(
            selected, 151.25, observation["observed_at"], source="dhan_last_trade"
        )
        assert quote_service.flush.await_count >= 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("replacement", [
        None,
        {"expiry": "2026-11-19", "strike": 23400.0, "option_type": "PE", "security_id": "53941"},
        {"expiry": "2026-11-12", "strike": 23500.0, "option_type": "PE", "security_id": "53941"},
        {"expiry": "2026-11-12", "strike": 23400.0, "option_type": "CE", "security_id": "53941"},
        {"expiry": "2026-11-12", "strike": 23400.0, "option_type": "PE", "security_id": "53942"},
    ])
    async def test_selected_quote_fetch_does_not_restore_old_selection(self, mock_supabase, replacement):
        """A response for a closed or replaced selection must not resurrect it."""
        import asyncio
        import main
        from unittest.mock import AsyncMock

        selected = {"expiry": "2026-11-12", "strike": 23400.0,
                    "option_type": "PE", "security_id": "53941"}
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch_observation(contract):
            started.set()
            await release.wait()
            return {"price": 151.25, "observed_at": "2026-11-03T04:55:01+00:00"}

        fetcher = MagicMock()
        fetcher.fetch_option_observation = AsyncMock(side_effect=fetch_observation)
        pm = MagicMock()
        pm.get_selected_option_contract.return_value = selected
        qs = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0)
        qs.record_option_quote(selected, 120, "2026-11-03T04:54:00Z")
        await qs.flush(force=True)
        task = asyncio.create_task(main._record_selected_option_quote(pm, fetcher, qs, "2026-11-05", {}))
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
            pm.get_selected_option_contract.return_value = replacement
            qs.reconcile_selected_contract(replacement)
            if replacement:
                qs.record_option_quote(replacement, 180, "2026-11-03T04:55:00Z")
            await qs.flush(force=True)
            previous_upserts = mock_supabase.table.return_value.upsert.call_count
            release.set()
            await asyncio.wait_for(task, timeout=1.0)
            await qs.flush(force=True)

            option = qs.get_latest_quotes_payload()["selectedOption"]
            if replacement is None:
                assert option is None
            else:
                assert option["expiry"] == replacement["expiry"]
                assert option["strike"] == replacement["strike"]
                assert option["optionType"] == replacement["option_type"]
                assert option["securityId"] == replacement["security_id"]
                assert option["currentPremium"] == 180
            later_upserts = mock_supabase.table.return_value.upsert.call_args_list[previous_upserts:]
            assert all(call.args[0]["price"] != 151.25 for call in later_upserts)
        finally:
            release.set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chain_failure", [False, True])
    async def test_rest_monitor_returns_while_selected_option_fetch_is_blocked(self, monkeypatch, chain_failure):
        """Dashboard quote latency must not hold up the monitoring loop."""
        import asyncio
        import main
        from unittest.mock import AsyncMock

        started, release = asyncio.Event(), asyncio.Event()
        selected = {"expiry": "2026-11-12", "strike": 23400.0,
                    "option_type": "PE", "security_id": "53941"}

        async def slow_fetch(contract):
            started.set()
            await release.wait()
            return {"price": 151.25, "observed_at": "2026-11-03T04:55:01+00:00"}

        fetcher = MagicMock()
        fetcher.fetch_latest_spot = AsyncMock(return_value=23350)
        fetcher.fetch_option_observation = AsyncMock(side_effect=slow_fetch)
        oi_fetcher = MagicMock()
        oi_fetcher.get_nearest_expiry = AsyncMock(return_value="2026-11-05")
        oi_fetcher.fetch_chain = AsyncMock(return_value=(None, {}))
        if chain_failure:
            oi_fetcher.fetch_chain.side_effect = ValueError("chain unavailable")
        pm = MagicMock()
        pm.active_trades = []
        pm.get_selected_option_contract.return_value = selected
        pm.update_trades = AsyncMock(return_value=[])
        dispatcher = MagicMock()
        dispatcher.on_spot = AsyncMock()
        dispatcher.on_snapshot = AsyncMock()
        qs = MagicMock()
        qs.flush = AsyncMock(return_value=True)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: qs)
        monkeypatch.setattr(main, "_session_now", lambda: datetime(2026, 11, 3, 10, 0))

        monitoring = asyncio.create_task(main._monitor_rest_without_candle(
            fetcher, oi_fetcher, pm, MagicMock(), dispatcher
        ))
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
            if chain_failure:
                with pytest.raises(ValueError, match="chain unavailable"):
                    await asyncio.wait_for(asyncio.shield(monitoring), timeout=0.25)
            else:
                await asyncio.wait_for(asyncio.shield(monitoring), timeout=0.25)
                dispatcher.on_snapshot.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(monitoring, return_exceptions=True)
            quote_task = getattr(pm, "_selected_option_quote_task", None)
            if isinstance(quote_task, asyncio.Task):
                await quote_task
            flush_task = getattr(pm, "_quote_flush_task", None)
            if isinstance(flush_task, asyncio.Task):
                await flush_task

    @pytest.mark.asyncio
    @pytest.mark.parametrize("queue_new_request", [False, True])
    async def test_background_option_requests_coalesce_and_use_new_selection(self, mock_supabase, queue_new_request):
        import asyncio
        import main
        from unittest.mock import AsyncMock

        first = {"expiry": "2026-11-12", "strike": 23400.0, "option_type": "PE", "security_id": "53941"}
        second = {"expiry": "2026-11-19", "strike": 23500.0, "option_type": "CE", "security_id": "53942"}
        started, release = asyncio.Event(), asyncio.Event()
        requested = []

        async def fetch(contract):
            requested.append(contract["security_id"])
            if len(requested) == 1:
                started.set()
                await release.wait()
            return {"price": 151.25, "observed_at": "2026-11-03T04:55:01+00:00"}

        fetcher = MagicMock()
        fetcher.fetch_option_observation = AsyncMock(side_effect=fetch)
        pm = MagicMock()
        pm.get_selected_option_contract.return_value = first
        qs = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0)
        task = main._schedule_selected_option_quote(pm, fetcher, qs, "2026-11-05", {})
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
            pm.get_selected_option_contract.return_value = second
            if queue_new_request:
                assert main._schedule_selected_option_quote(pm, fetcher, qs, "2026-11-05", {}) is task
                assert main._schedule_selected_option_quote(pm, fetcher, qs, "2026-11-05", {}) is task
            release.set()
            await asyncio.wait_for(task, timeout=1.0)
            await pm._quote_flush_task
            assert requested == ["53941", "53942"]
            assert qs.get_latest_quotes_payload()["selectedOption"]["securityId"] == "53942"
        finally:
            release.set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selection", ["same", "none", "replacement"])
    async def test_background_option_quote_retries_deferral_only_for_current_selection(
        self, mock_supabase, monkeypatch, selection
    ):
        import asyncio
        import main
        from fetchers.price_fetcher import ReferencePriceDeferred
        from unittest.mock import AsyncMock

        selected = {"expiry": "2026-11-12", "strike": 23400.0, "option_type": "PE", "security_id": "53941"}
        replacement = {"expiry": "2026-11-19", "strike": 23500.0, "option_type": "CE", "security_id": "53942"}
        waiting, release = asyncio.Event(), asyncio.Event()

        async def wait_for_cooldown(delay):
            waiting.set()
            await release.wait()

        monkeypatch.setattr(main, "_quote_retry_sleep", wait_for_cooldown)
        fetcher = MagicMock()
        fetcher.fetch_option_observation = AsyncMock(side_effect=[
            ReferencePriceDeferred("protective cooldown"),
            {"price": 151.25, "observed_at": "2026-11-03T04:55:01+00:00"},
        ])
        pm = MagicMock()
        pm.get_selected_option_contract.return_value = selected
        qs = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0)
        task = main._schedule_selected_option_quote(pm, fetcher, qs, "2026-11-05", {})
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1.0)
            if selection == "none":
                pm.get_selected_option_contract.return_value = None
            elif selection == "replacement":
                pm.get_selected_option_contract.return_value = replacement
            release.set()
            await asyncio.wait_for(task, timeout=1.0)
            await pm._quote_flush_task
            assert fetcher.fetch_option_observation.await_count == (1 if selection == "none" else 2)
            option = qs.get_latest_quotes_payload()["selectedOption"]
            if selection == "none":
                assert option is None
            else:
                assert option["securityId"] == ("53942" if selection == "replacement" else "53941")
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_queued_chain_quote_keeps_its_original_observation_time(self, mock_supabase, monkeypatch):
        import asyncio
        import main
        from unittest.mock import AsyncMock

        first = {"expiry": "2026-11-12", "strike": 23400.0, "option_type": "PE", "security_id": "53941"}
        second = {"expiry": "2026-11-19", "strike": 23500.0, "option_type": "CE", "security_id": "53942"}
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(contract):
            started.set()
            await release.wait()
            return {"price": 151.25, "observed_at": "2026-11-03T04:55:01+00:00"}

        def chain_quote(contract, chain, *, timestamp):
            return {"price": 180, "observed_at": timestamp}

        monkeypatch.setattr("quote_service.match_option_quote_from_chain", chain_quote)
        fetcher = MagicMock()
        fetcher.fetch_option_observation = AsyncMock(side_effect=fetch)
        pm = MagicMock()
        pm.get_selected_option_contract.return_value = first
        qs = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0)
        task = main._schedule_selected_option_quote(pm, fetcher, qs, "2026-11-05", {})
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
            pm.get_selected_option_contract.return_value = second
            before = datetime.now(timezone.utc)
            main._schedule_selected_option_quote(pm, fetcher, qs, second["expiry"], {})
            after = datetime.now(timezone.utc)
            release.set()
            await asyncio.wait_for(task, timeout=1.0)
            await pm._quote_flush_task
            observed = datetime.fromisoformat(qs._latest_option["observed_at"].replace("Z", "+00:00"))
            assert before <= observed <= after
            assert qs.get_latest_quotes_payload()["selectedOption"]["securityId"] == "53942"
        finally:
            release.set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timeout", [False, True])
    async def test_selected_option_quote_shutdown_drain(self, timeout):
        import asyncio
        import main

        started, release = asyncio.Event(), asyncio.Event()

        async def capture():
            started.set()
            await release.wait()

        pm = MagicMock()
        task = asyncio.create_task(capture())
        pm._selected_option_quote_task = task
        await asyncio.wait_for(started.wait(), timeout=1.0)
        if not timeout:
            release.set()
        assert await main._drain_selected_option_quote(pm, timeout_seconds=0.01) is not timeout
        assert task.cancelled() is timeout

    @pytest.mark.asyncio
    async def test_rest_fallback_runs_protective_check_while_quote_flush_is_blocked(self, monkeypatch):
        """A slow quote write must not hold up the REST-based exit check."""
        import asyncio
        import main
        from unittest.mock import AsyncMock

        flush_started = asyncio.Event()
        release_flush = asyncio.Event()
        protective_check_done = asyncio.Event()

        async def slow_flush(**kwargs):
            flush_started.set()
            await release_flush.wait()
            return True

        async def update_trades(*args, **kwargs):
            await asyncio.sleep(0)
            protective_check_done.set()
            return []

        quote_service = MagicMock()
        quote_service.flush = AsyncMock(side_effect=slow_flush)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: quote_service)

        price_fetcher = MagicMock()
        price_fetcher.fetch_latest_spot = AsyncMock(return_value=23350.0)
        position_manager = MagicMock()
        position_manager.active_trades = []
        position_manager.update_trades = AsyncMock(side_effect=update_trades)
        dispatcher = MagicMock()
        dispatcher.on_spot = AsyncMock()
        monkeypatch.setattr(main, "_session_now", lambda: datetime(2026, 11, 3, 15, 31))

        monitoring_task = asyncio.create_task(main._monitor_rest_without_candle(
            price_fetcher, MagicMock(), position_manager, MagicMock(), dispatcher
        ))
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)
        try:
            await asyncio.wait_for(protective_check_done.wait(), timeout=0.25)
            protected_before_flush = True
        except asyncio.TimeoutError:
            protected_before_flush = False
        release_flush.set()
        await asyncio.wait_for(monitoring_task, timeout=1.0)
        quote_flush_task = getattr(position_manager, "_quote_flush_task", None)
        if quote_flush_task is not None:
            await quote_flush_task

        assert protected_before_flush is True

    @pytest.mark.asyncio
    async def test_completed_candle_runs_protection_while_quote_flush_is_blocked(self, monkeypatch):
        """A slow candle quote write must not hold up option ATR monitoring."""
        import asyncio
        import main
        from unittest.mock import AsyncMock

        flush_started = asyncio.Event()
        release_flush = asyncio.Event()
        protective_check_done = asyncio.Event()

        async def slow_flush(**kwargs):
            flush_started.set()
            await release_flush.wait()
            return True

        async def monitor_completed_atr(*args, **kwargs):
            await asyncio.sleep(0)
            protective_check_done.set()
            return False

        quote_service = MagicMock()
        quote_service.flush = AsyncMock(side_effect=slow_flush)
        monkeypatch.setattr("quote_service.get_quote_service", lambda *args, **kwargs: quote_service)
        monkeypatch.setattr(main, "_monitor_completed_atr", monitor_completed_atr)

        candle = MagicMock()
        candle.close = 23350.0
        candle.timestamp = datetime.now(timezone.utc)
        price_fetcher = MagicMock()
        price_fetcher.fetch_latest_candle = AsyncMock(return_value=candle)
        position_manager = MagicMock()
        position_manager.active_trades = []

        monitoring_task = asyncio.create_task(main._fetch_candle_with_protection(
            price_fetcher, position_manager, dispatcher=None
        ))
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)
        try:
            await asyncio.wait_for(protective_check_done.wait(), timeout=0.25)
            protected_before_flush = True
        except asyncio.TimeoutError:
            protected_before_flush = False
        release_flush.set()
        result = await asyncio.wait_for(monitoring_task, timeout=1.0)
        quote_flush_task = getattr(position_manager, "_quote_flush_task", None)
        if quote_flush_task is not None:
            await quote_flush_task

        assert result is candle
        assert protected_before_flush is True

    @pytest.mark.asyncio
    async def test_quote_flush_shutdown_drain_cancels_timed_out_task(self):
        """A quote flush that exceeds the shutdown budget is cancelled cleanly."""
        import asyncio
        import main
        from unittest.mock import AsyncMock

        flush_started = asyncio.Event()
        never_release = asyncio.Event()

        async def slow_flush(**kwargs):
            flush_started.set()
            await never_release.wait()

        quote_service = MagicMock()
        quote_service.flush = AsyncMock(side_effect=slow_flush)
        pm = MagicMock()
        task = main._schedule_quote_flush(pm, quote_service)
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)

        drained = await main._drain_quote_flush(pm, timeout_seconds=0.01)

        assert drained is False
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_new_scheduled_flush_waits_out_throttle_and_persists_option(self):
        """A second task persists a new option even without another market event."""
        import asyncio
        import main
        from quote_service import QuotePersistenceService

        supabase = MagicMock()
        service = QuotePersistenceService(supabase_client=supabase, throttle_seconds=0.2)
        pm = MagicMock()
        service.record_spot_quote(24000, "2026-11-03T05:00:00Z")
        await main._schedule_quote_flush(pm, service)

        contract = {"expiry": "2026-11-05", "strike": 24000, "option_type": "CE", "security_id": "53940"}
        service.record_option_quote(contract, 150, "2026-11-03T05:00:01Z")
        await asyncio.wait_for(main._schedule_quote_flush(pm, service), timeout=2.0)

        rows = [call.args[0] for call in supabase.table.return_value.upsert.call_args_list]
        assert any(row["instrument_id"] == "OPTION:SELECTED" and row["price"] == 150 for row in rows)
        assert service._dirty is False

    @pytest.mark.asyncio
    async def test_scheduled_flush_rechecks_throttle_after_forced_write(self, monkeypatch):
        """A forced write during the wait must not strand the next snapshot."""
        import asyncio
        import time
        import main
        import quote_service
        from quote_service import QuotePersistenceService

        current_time = [1000.0]
        monkeypatch.setattr(quote_service.time, "monotonic", lambda: current_time[0])

        wait_started = asyncio.Event()
        release_wait = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay):
            if not wait_started.is_set():
                wait_started.set()
                await release_wait.wait()
            else:
                current_time[0] += delay
                await real_sleep(0)

        monkeypatch.setattr(quote_service, "async_sleep", controlled_sleep)
        supabase = MagicMock()
        service = QuotePersistenceService(supabase_client=supabase, throttle_seconds=0.2)
        pm = MagicMock()
        service.record_spot_quote(24000, "2026-11-03T05:00:00Z")
        await service.flush(force=True)
        service.record_spot_quote(24001, "2026-11-03T05:00:01Z")
        task = main._schedule_quote_flush(pm, service)
        await asyncio.wait_for(wait_started.wait(), timeout=1.0)
        current_time[0] += 0.05
        await service.flush(force=True)
        service.record_spot_quote(24002, "2026-11-03T05:00:02Z")
        release_wait.set()

        await asyncio.wait_for(task, timeout=2.0)

        rows = [call.args[0] for call in supabase.table.return_value.upsert.call_args_list]
        assert rows[-1]["price"] == 24002
        assert service._dirty is False

    @pytest.mark.asyncio
    async def test_scheduled_quote_flush_coalesces_updates_during_inflight_write(self):
        """A quote recorded during a slow flush gets a follow-up snapshot."""
        import asyncio
        import main
        from unittest.mock import AsyncMock

        flush_started = asyncio.Event()
        release_flush = asyncio.Event()
        latest_price = {"value": 24000.0}
        snapshots = []

        async def flush_snapshot(**kwargs):
            snapshots.append(latest_price["value"])
            if len(snapshots) == 1:
                flush_started.set()
                await release_flush.wait()

        quote_service = MagicMock()
        quote_service.flush = AsyncMock(side_effect=flush_snapshot)
        pm = MagicMock()
        task = main._schedule_quote_flush(pm, quote_service)
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)

        latest_price["value"] = 24020.0
        assert main._schedule_quote_flush(pm, quote_service) is task
        release_flush.set()
        await task

        assert snapshots == [24000.0, 24020.0]

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

