import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest

from quote_service import (
    classify_quote_freshness,
    format_frontend_quote_contract,
    is_quote_stale,
    match_option_quote_from_chain,
    QuotePersistenceService,
    validate_option_contract,
    _to_utc_iso,
    _parse_utc_datetime,
)


def _stateful_quote_supabase(initial_rows):
    """Provide a small Supabase double that exposes writes through the quotes RPC."""
    persisted_rows = {row["instrument_id"]: dict(row) for row in initial_rows}
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table

    def upsert(row):
        query = MagicMock()

        def execute():
            persisted_rows[row["instrument_id"]] = dict(row)
            return MagicMock(data=[])

        query.execute.side_effect = execute
        return query

    def delete():
        query = MagicMock()
        query.eq.return_value = query

        def execute():
            persisted_rows.pop("OPTION:SELECTED", None)
            return MagicMock(data=[])

        query.execute.side_effect = execute
        return query

    query = MagicMock()

    def execute_rpc():
        option = persisted_rows.get("OPTION:SELECTED")
        selected_option = None
        if option and option.get("security_id"):
            selected_option = {
                "securityId": option["security_id"],
                "currentPremium": option["price"],
            }
        return MagicMock(data={"selectedOption": selected_option})

    query.execute.side_effect = execute_rpc

    table.upsert.side_effect = upsert
    table.delete.side_effect = delete
    client.rpc.return_value = query
    return client, persisted_rows


class TestOptionContractValidation:
    """Rigorous tests ensuring option contracts are strictly identified without guessing."""

    def test_valid_contract(self):
        contract = {
            "expiry_date": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        validated = validate_option_contract(contract)
        assert validated is not None
        assert validated["expiry"] == "2026-11-03"
        assert validated["strike"] == 23300.0
        assert validated["option_type"] == "CE"
        assert validated["security_id"] == "53940"

    def test_valid_contract_alternate_field_names(self):
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300.5,
            "optionType": "pe",
            "securityId": "53941",
        }
        validated = validate_option_contract(contract)
        assert validated is not None
        assert validated["expiry"] == "2026-11-03"
        assert validated["strike"] == 23300.5
        assert validated["option_type"] == "PE"
        assert validated["security_id"] == "53941"

    def test_missing_expiry_rejected(self):
        contract = {
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        assert validate_option_contract(contract) is None

    def test_invalid_expiry_format_rejected(self):
        contract = {
            "expiry": "03-11-2026",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        assert validate_option_contract(contract) is None

    def test_missing_strike_rejected(self):
        contract = {
            "expiry": "2026-11-03",
            "option_type": "CE",
            "security_id": "53940",
        }
        assert validate_option_contract(contract) is None

    def test_non_positive_strike_rejected(self):
        for bad_strike in [0, -100, float("nan"), float("inf")]:
            contract = {
                "expiry": "2026-11-03",
                "strike": bad_strike,
                "option_type": "CE",
                "security_id": "53940",
            }
            assert validate_option_contract(contract) is None

    def test_missing_option_type_rejected(self):
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "security_id": "53940",
        }
        assert validate_option_contract(contract) is None

    def test_invalid_option_type_rejected(self):
        for bad_type in ["CALL", "PUT", "XX", "", None]:
            contract = {
                "expiry": "2026-11-03",
                "strike": 23300,
                "option_type": bad_type,
                "security_id": "53940",
            }
            assert validate_option_contract(contract) is None

    def test_missing_security_id_rejected(self):
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
        }
        assert validate_option_contract(contract) is None

    def test_non_digit_security_id_rejected(self):
        for bad_sec_id in ["ABC", "53a40", "", None, "   "]:
            contract = {
                "expiry": "2026-11-03",
                "strike": 23300,
                "option_type": "CE",
                "security_id": bad_sec_id,
            }
            assert validate_option_contract(contract) is None

    def test_empty_or_non_dict_rejected(self):
        assert validate_option_contract({}) is None
        assert validate_option_contract(None) is None
        assert validate_option_contract("contract") is None


class TestStaleQuoteBehavior:
    """Tests stale detection and freshness classification."""

    def test_fresh_quote(self):
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)
        observed = now - timedelta(seconds=4)
        assert is_quote_stale(observed, threshold_seconds=10.0, reference_time=now) is False
        assert classify_quote_freshness(observed, reference_time=now) == "FRESH"

    def test_stale_quote_boundary(self):
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)
        observed = now - timedelta(seconds=15)
        assert is_quote_stale(observed, threshold_seconds=10.0, reference_time=now) is True
        assert classify_quote_freshness(observed, reference_time=now) == "STALE"

    def test_expired_quote(self):
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)
        observed = now - timedelta(seconds=90)
        assert is_quote_stale(observed, threshold_seconds=10.0, reference_time=now) is True
        assert classify_quote_freshness(observed, reference_time=now) == "EXPIRED"

    def test_iso_string_timestamp(self):
        now = datetime(2026, 11, 2, 4, 52, 10, tzinfo=timezone.utc)
        iso_str = "2026-11-02T04:52:00Z"
        assert is_quote_stale(iso_str, threshold_seconds=15.0, reference_time=now) is False
        assert is_quote_stale(iso_str, threshold_seconds=5.0, reference_time=now) is True

    def test_malformed_timestamp_considered_expired(self):
        assert is_quote_stale("invalid-date") is True
        assert classify_quote_freshness("invalid-date") == "EXPIRED"
        assert is_quote_stale(None) is True
        assert classify_quote_freshness(None) == "EXPIRED"


class TestFrontendContractFormatting:
    """Verifies that the generated contract matches SpanQuant frontend requirements exactly."""

    def test_full_contract_format(self):
        underlying = {
            "symbol": "NIFTY",
            "price": 23300.8,
            "observed_at": "2026-11-02T04:52:00Z",
            "source": "dhan_rest_ltp",
        }
        option = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
            "price": 125.5,
            "observed_at": "2026-11-02T04:52:00Z",
            "source": "dhan_quote_data",
        }
        contract = format_frontend_quote_contract(underlying, option)
        assert contract == {
            "underlying": {
                "symbol": "NIFTY",
                "currentSpot": 23300.8,
                "updatedAt": "2026-11-02T04:52:00Z",
            },
            "selectedOption": {
                "expiry": "2026-11-03",
                "strike": 23300,
                "optionType": "CE",
                "securityId": "53940",
                "currentPremium": 125.5,
                "updatedAt": "2026-11-02T04:52:00Z",
            },
        }

    def test_null_option_when_unselected(self):
        underlying = {
            "symbol": "NIFTY",
            "price": 23300.8,
            "observed_at": "2026-11-02T04:52:00Z",
            "source": "dhan_rest_ltp",
        }
        contract = format_frontend_quote_contract(underlying, None)
        assert contract["underlying"]["currentSpot"] == 23300.8
        assert contract["selectedOption"] is None

    def test_null_when_underlying_missing(self):
        contract = format_frontend_quote_contract(None, None)
        assert contract == {
            "underlying": None,
            "selectedOption": None,
        }

    def test_distinct_values_from_entry_and_signal(self):
        """Ensure live quotes do not mirror or overwrite trade entry or signal prices."""
        trade_entry_spot = 23250.0
        trade_entry_premium = 85.0
        live_spot = 23300.8
        live_premium = 125.5

        underlying = {
            "symbol": "NIFTY",
            "price": live_spot,
            "observed_at": "2026-11-02T04:52:00Z",
            "source": "dhan_websocket_tick",
        }
        option = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
            "price": live_premium,
            "observed_at": "2026-11-02T04:52:00Z",
            "source": "dhan_quote_data",
        }
        contract = format_frontend_quote_contract(underlying, option)
        assert contract["underlying"]["currentSpot"] != trade_entry_spot
        assert contract["underlying"]["currentSpot"] == 23300.8
        assert contract["selectedOption"]["currentPremium"] != trade_entry_premium
        assert contract["selectedOption"]["currentPremium"] == 125.5


class TestChainOptionMatching:
    """Tests extracting option quotes from the full option chain."""

    def test_match_ce_quote_from_chain(self):
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        chain = [
            {"strike": 23200, "expiry_date": "2026-11-03", "ce_security_id": "53938", "ce_ltp": 160.0},
            {"strike": 23300, "expiry_date": "2026-11-03", "ce_security_id": "53940", "ce_ltp": 86.7, "pe_ltp": 72.3},
        ]
        match = match_option_quote_from_chain(contract, chain, timestamp="2026-11-02T04:52:00Z")
        assert match is not None
        assert match["price"] == 86.7
        assert match["security_id"] == "53940"
        assert match["source"] == "dhan_option_chain"

    def test_match_pe_quote_from_chain(self):
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "PE",
            "security_id": "53941",
        }
        chain = [
            {"strike": 23300, "expiry_date": "2026-11-03", "pe_security_id": "53941", "pe_ltp": 74.5},
        ]
        match = match_option_quote_from_chain(contract, chain)
        assert match is not None
        assert match["price"] == 74.5

    def test_unmatched_returns_none(self):
        contract = {
            "expiry": "2026-11-03",
            "strike": 23500,
            "option_type": "CE",
            "security_id": "53960",
        }
        chain = [
            {"strike": 23300, "expiry_date": "2026-11-03", "ce_security_id": "53940", "ce_ltp": 86.7},
        ]
        assert match_option_quote_from_chain(contract, chain) is None


class TestQuotePersistenceService:
    """Tests quote persistence, throttling, Supabase upserting, and clean lifecycle."""

    @pytest.mark.asyncio
    async def test_in_memory_quotes_and_payload(self):
        service = QuotePersistenceService(supabase_client=None, throttle_seconds=1.0)
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)

        # Record spot
        service.record_spot_quote(23300.8, now, source="dhan_rest_ltp")
        payload = service.get_latest_quotes_payload()
        assert payload["underlying"]["currentSpot"] == 23300.8
        assert payload["underlying"]["updatedAt"] == "2026-11-02T04:52:00Z"
        assert payload["selectedOption"] is None

        # Record option
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        service.record_option_quote(contract, 125.5, now, source="dhan_quote_api")
        payload = service.get_latest_quotes_payload()
        assert payload["selectedOption"]["currentPremium"] == 125.5
        assert payload["selectedOption"]["securityId"] == "53940"
        assert payload["selectedOption"]["expiry"] == "2026-11-03"

    @pytest.mark.asyncio
    async def test_throttled_flush_avoids_per_tick_db_inserts(self):
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_upsert = MagicMock()
        mock_table.upsert.return_value = mock_upsert
        mock_upsert.execute.return_value = MagicMock(data=[])

        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=1.0)
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)

        # Tick 1: record and flush
        service.record_spot_quote(23300.0, now)
        flushed_1 = await service.flush(force=True)
        assert flushed_1 is True
        assert mock_table.upsert.call_count == 1

        # Check upsert payload structure
        upserted_row = mock_table.upsert.call_args[0][0]
        assert upserted_row["instrument_id"] == "UNDERLYING:NIFTY"
        assert upserted_row["instrument_type"] == "UNDERLYING"
        assert upserted_row["price"] == 23300.0

        # Tick 2 immediately after: throttled!
        service.record_spot_quote(23300.5, now + timedelta(milliseconds=100))
        flushed_2 = await service.flush(force=False)
        assert flushed_2 is False
        assert mock_table.upsert.call_count == 1  # Still 1, NOT called!

        # After throttle period expires:
        service._last_persist_time -= 2.0  # Simulate 2s elapsed
        flushed_3 = await service.flush(force=False)
        assert flushed_3 is True
        assert mock_table.upsert.call_count == 2
        assert mock_table.upsert.call_args[0][0]["price"] == 23300.5

    @pytest.mark.asyncio
    async def test_option_quote_upsert_payload(self):
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_upsert = MagicMock()
        mock_table.upsert.return_value = mock_upsert
        mock_upsert.execute.return_value = MagicMock(data=[])

        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        now = datetime(2026, 11, 2, 4, 52, 0, tzinfo=timezone.utc)

        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        service.record_spot_quote(23300.8, now)
        service.record_option_quote(contract, 125.5, now)

        await service.flush(force=True)
        assert mock_table.upsert.call_count == 2
        upserted_rows = [call.args[0] for call in mock_table.upsert.call_args_list]

        underlying_row = next(r for r in upserted_rows if r["instrument_id"] == "UNDERLYING:NIFTY")
        assert underlying_row["price"] == 23300.8
        assert underlying_row["instrument_type"] == "UNDERLYING"
        assert isinstance(underlying_row["raw_metadata"], dict)

        option_row = next(r for r in upserted_rows if r["instrument_id"] == "OPTION:SELECTED")
        assert option_row["price"] == 125.5
        assert option_row["instrument_type"] == "OPTION"
        assert option_row["expiry"] == "2026-11-03"
        assert option_row["strike"] == 23300.0
        assert option_row["option_type"] == "CE"
        assert option_row["security_id"] == "53940"
        assert isinstance(option_row["raw_metadata"], dict)

    @pytest.mark.asyncio
    async def test_pending_option_deletion_retained_on_failure(self):
        """Verifies that transient Supabase failures do not lose pending option deletion."""
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.delete.side_effect = RuntimeError("Delete network failure")

        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        service.record_option_quote(contract, 125.5, "2026-11-02T04:52:00Z")
        service.clear_selected_option()
        assert service._clear_option_pending is True

        # Flush fails due to delete failure
        success = await service.flush(force=True)
        assert success is False
        # Deletion flag must be preserved so next retry deletes it
        assert service._clear_option_pending is True

        # But if a new option is recorded before next retry, don't delete new option
        service.record_option_quote(contract, 130.0, "2026-11-02T04:53:00Z")
        assert service._clear_option_pending is False

    @pytest.mark.asyncio
    async def test_clear_selected_option(self):
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_delete = MagicMock()
        mock_table.delete.return_value = mock_delete
        mock_eq = MagicMock()
        mock_delete.eq.return_value = mock_eq
        mock_eq.execute.return_value = MagicMock(data=[])

        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        service.record_option_quote(contract, 125.5, "2026-11-02T04:52:00Z")
        assert service.get_latest_quotes_payload()["selectedOption"] is not None

        # Clear option
        service.clear_selected_option()
        assert service.get_latest_quotes_payload()["selectedOption"] is None
        await service.flush(force=True)
        # Should delete OPTION:SELECTED row so dashboard doesn't see old contract
        assert mock_table.delete.call_count == 1
        mock_delete.eq.assert_called_with("instrument_id", "OPTION:SELECTED")

    @pytest.mark.asyncio
    async def test_clear_selected_option_when_memory_empty(self):
        """Verifies clearing persisted option from Supabase even when local cache is empty after restart."""
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_delete = MagicMock()
        mock_table.delete.return_value = mock_delete
        mock_eq = MagicMock()
        mock_delete.eq.return_value = mock_eq
        mock_eq.execute.return_value = MagicMock(data=[])

        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        assert service._latest_option is None

        # Call clear on empty memory
        service.clear_selected_option()
        assert service._clear_option_pending is True

        await service.flush(force=True)
        assert mock_table.delete.call_count == 1
        mock_delete.eq.assert_called_with("instrument_id", "OPTION:SELECTED")

    def test_record_spot_quote_rejects_older_observation(self):
        """Reject older historical candle closes from overwriting newer live tick quotes."""
        service = QuotePersistenceService()
        newer_time = datetime(2026, 11, 2, 4, 55, 10, tzinfo=timezone.utc)
        older_time = datetime(2026, 11, 2, 4, 55, 0, tzinfo=timezone.utc)

        # Record fresh tick at 04:55:10
        assert service.record_spot_quote(23310.0, newer_time) is True
        assert service.get_latest_quotes_payload()["underlying"]["currentSpot"] == 23310.0

        # Attempt to record older candle close from 04:55:00
        assert service.record_spot_quote(23305.0, older_time) is False
        # Must retain newer price and timestamp
        assert service.get_latest_quotes_payload()["underlying"]["currentSpot"] == 23310.0
        assert service.get_latest_quotes_payload()["underlying"]["updatedAt"] == "2026-11-02T04:55:10Z"

    @pytest.mark.asyncio
    async def test_record_spot_quote_orders_subsecond_observations_before_frontend_truncation(self):
        """Keep subsecond precision for ordering and persistence, while formatting UI time to seconds."""
        mock_supabase = MagicMock()
        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        newer_time = datetime(2026, 11, 3, 5, 0, 0, 900000, tzinfo=timezone.utc)
        older_time = datetime(2026, 11, 3, 5, 0, 0, 100000, tzinfo=timezone.utc)

        assert service.record_spot_quote(24020.0, newer_time) is True
        assert service.record_spot_quote(24000.0, older_time) is False
        assert service.get_latest_quotes_payload()["underlying"]["currentSpot"] == 24020.0
        assert service.get_latest_quotes_payload()["underlying"]["updatedAt"] == "2026-11-03T05:00:00Z"

        await service.flush(force=True)

        persisted = mock_supabase.table.return_value.upsert.call_args.args[0]
        assert persisted["observed_at"] == "2026-11-03T05:00:00.900000Z"

        older_first = QuotePersistenceService()
        assert older_first.record_spot_quote(24000.0, older_time) is True
        assert older_first.record_spot_quote(24020.0, newer_time) is True
        assert older_first.get_latest_quotes_payload()["underlying"]["currentSpot"] == 24020.0

    def test_record_option_quote_rejects_older_observation(self):
        """Reject older historical option quotes from overwriting newer live quotes."""
        service = QuotePersistenceService()
        newer_time = datetime(2026, 11, 2, 4, 55, 10, tzinfo=timezone.utc)
        older_time = datetime(2026, 11, 2, 4, 55, 0, tzinfo=timezone.utc)
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }

        # Record newer quote
        assert service.record_option_quote(contract, 125.5, newer_time) is True
        assert service.get_latest_quotes_payload()["selectedOption"]["currentPremium"] == 125.5

        # Attempt older quote
        assert service.record_option_quote(contract, 120.0, older_time) is False
        assert service.get_latest_quotes_payload()["selectedOption"]["currentPremium"] == 125.5
        assert service.get_latest_quotes_payload()["selectedOption"]["updatedAt"] == "2026-11-02T04:55:10Z"

    @pytest.mark.asyncio
    async def test_record_option_quote_orders_subsecond_observations_before_frontend_truncation(self):
        """Do not let an older same-second option quote replace the newer cached premium."""
        mock_supabase = MagicMock()
        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        contract = {
            "expiry": "2026-11-03",
            "strike": 23300,
            "option_type": "CE",
            "security_id": "53940",
        }
        newer_time = datetime(2026, 11, 3, 5, 0, 0, 900000, tzinfo=timezone.utc)
        older_time = datetime(2026, 11, 3, 5, 0, 0, 100000, tzinfo=timezone.utc)

        assert service.record_option_quote(contract, 125.5, newer_time) is True
        assert service.record_option_quote(contract, 120.0, older_time) is False
        assert service.get_latest_quotes_payload()["selectedOption"]["currentPremium"] == 125.5
        assert service.get_latest_quotes_payload()["selectedOption"]["updatedAt"] == "2026-11-03T05:00:00Z"

        await service.flush(force=True)

        persisted = mock_supabase.table.return_value.upsert.call_args.args[0]
        assert persisted["observed_at"] == "2026-11-03T05:00:00.900000Z"

        older_first = QuotePersistenceService()
        assert older_first.record_option_quote(contract, 120.0, older_time) is True
        assert older_first.record_option_quote(contract, 125.5, newer_time) is True
        assert older_first.get_latest_quotes_payload()["selectedOption"]["currentPremium"] == 125.5

    @pytest.mark.asyncio
    async def test_supabase_error_does_not_crash(self):
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.upsert.side_effect = RuntimeError("Supabase network timeout")

        service = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        service.record_spot_quote(23300.8, "2026-11-02T04:52:00Z")

        # Flusher must catch error and return False without raising
        success = await service.flush(force=True)
        assert success is False
        # Memory payload remains intact
        assert service.get_latest_quotes_payload()["underlying"]["currentSpot"] == 23300.8

    def test_ist_timestamp_normalization(self):
        """Dhan candle feeds arrive as naive IST; must convert to UTC properly without +5.5h shift."""
        # Naive datetime 10:25:00 IST -> 04:55:00 UTC
        naive_dt = datetime(2026, 11, 2, 10, 25, 0)
        iso_str = _to_utc_iso(naive_dt)
        assert iso_str == "2026-11-02T04:55:00Z"

        # Naive ISO string "2026-11-02 10:25:00" -> 04:55:00 UTC
        iso_str_text = _to_utc_iso("2026-11-02 10:25:00")
        assert iso_str_text == "2026-11-02T04:55:00Z"

        # Explicit UTC string preserves UTC
        utc_text = _to_utc_iso("2026-11-02T04:55:00Z")
        assert utc_text == "2026-11-02T04:55:00Z"

        # Parse UTC datetime
        parsed = _parse_utc_datetime(naive_dt)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.hour == 4
        assert parsed.minute == 55

    def test_get_supabase_client_initialization(self, monkeypatch):
        """Verifies quote_service initializes with a real client or through fallback."""
        from unittest.mock import MagicMock
        import quote_service

        quote_service._global_quote_service = None
        mock_client = MagicMock()
        monkeypatch.setattr("storage.get_supabase_client", lambda: mock_client)

        svc = quote_service.get_quote_service()
        assert svc.supabase is mock_client

        # If re-called with explicit client, keeps or updates
        new_client = MagicMock()
        svc2 = quote_service.get_quote_service(new_client)
        assert svc2 is svc

    def test_are_option_contracts_equal(self):
        """Verifies are_option_contracts_equal properly checks all 4 contract identifiers."""
        from quote_service import are_option_contracts_equal

        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c2 = {"expiry": "2026-11-05", "strike": 23300.0, "option_type": "ce", "security_id": "53940"}
        c_diff_strike = {"expiry": "2026-11-05", "strike": 23400, "option_type": "CE", "security_id": "53940"}
        c_diff_type = {"expiry": "2026-11-05", "strike": 23300, "option_type": "PE", "security_id": "53940"}
        c_diff_expiry = {"expiry": "2026-11-12", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c_diff_sec = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53941"}

        assert are_option_contracts_equal(c1, c2) is True
        assert are_option_contracts_equal(c1, c_diff_strike) is False
        assert are_option_contracts_equal(c1, c_diff_type) is False
        assert are_option_contracts_equal(c1, c_diff_expiry) is False
        assert are_option_contracts_equal(c1, c_diff_sec) is False
        assert are_option_contracts_equal(c1, None) is False
        assert are_option_contracts_equal(None, None) is False

    def test_reconcile_selected_contract_clears_differing_contract(self):
        """Verifies reconciling a differing selected contract clears the cached quote and schedules deletion."""
        svc = QuotePersistenceService(supabase_client=MagicMock())
        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c2 = {"expiry": "2026-11-12", "strike": 23400, "option_type": "PE", "security_id": "53941"}

        svc.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")
        assert svc.get_cached_option_contract() is not None
        assert svc.get_cached_option_contract()["security_id"] == "53940"

        # Reconciling with different contract B clears contract A
        cleared = svc.reconcile_selected_contract(c2)
        assert cleared is True
        assert svc.get_cached_option_contract() is None
        assert svc._clear_option_pending is True
        assert svc._dirty is True

    def test_reconcile_selected_contract_retains_identical_contract(self):
        """Verifies reconciling an identical selected contract preserves the cached quote."""
        svc = QuotePersistenceService(supabase_client=MagicMock())
        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c1_alt = {"expiry": "2026-11-05", "strike": 23300.0, "option_type": "ce", "security_id": "53940"}

        svc.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")
        cleared = svc.reconcile_selected_contract(c1_alt)
        assert cleared is False
        assert svc.get_cached_option_contract() is not None
        assert svc._clear_option_pending is False

    def test_reconcile_selected_contract_clears_when_none(self):
        """Verifies reconciling with None clears the cached quote and schedules deletion."""
        svc = QuotePersistenceService(supabase_client=MagicMock())
        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}

        svc.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")
        cleared = svc.reconcile_selected_contract(None)
        assert cleared is True
        assert svc.get_cached_option_contract() is None
        assert svc._clear_option_pending is True

    @pytest.mark.asyncio
    async def test_reconcile_selected_contract_deletes_unknown_persisted_quote_once(self):
        """Startup removes a persisted old selection until a quote for the restored contract arrives."""
        old_row = {
            "instrument_id": "OPTION:SELECTED", "instrument_type": "OPTION", "symbol": "NIFTY",
            "price": 125.0, "observed_at": "2026-11-03T04:54:00Z", "source": "dhan_quote_api",
            "expiry": "2026-11-05", "strike": 23300.0, "option_type": "CE",
            "security_id": "53940", "raw_metadata": {},
        }
        mock_supabase, persisted_rows = _stateful_quote_supabase([old_row])
        svc = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        selected = {
            "expiry": "2026-11-12", "strike": 23400,
            "option_type": "PE", "security_id": "53941",
        }

        assert mock_supabase.rpc("get_latest_market_quotes").execute().data["selectedOption"]["securityId"] == "53940"
        assert svc.reconcile_selected_contract(selected) is True
        assert svc.get_cached_option_contract() is None
        assert svc._clear_option_pending is True
        assert svc.reconcile_selected_contract(selected) is False

        await svc.flush(force=True)

        mock_supabase.table.return_value.delete.assert_called_once()
        assert persisted_rows == {}
        assert mock_supabase.rpc("get_latest_market_quotes").execute().data["selectedOption"] is None
        assert svc._clear_option_pending is False
        assert svc.reconcile_selected_contract(selected) is False

        assert svc.record_option_quote(selected, 151.25, "2026-11-03T04:55:01+00:00") is True
        await svc.flush(force=True)
        selected_option = mock_supabase.rpc("get_latest_market_quotes").execute().data["selectedOption"]
        assert selected_option["securityId"] == "53941"
        assert selected_option["currentPremium"] == 151.25

        svc.record_spot_quote(23401.0, "2026-11-03T04:55:02+00:00")
        await svc.flush(force=True)
        selected_option = mock_supabase.rpc("get_latest_market_quotes").execute().data["selectedOption"]
        assert selected_option["securityId"] == "53941"
        assert persisted_rows["OPTION:SELECTED"]["security_id"] == "53941"

    @pytest.mark.asyncio
    async def test_reconcile_selected_contract_preserves_quote_arriving_before_delete_flush(self):
        """A fresh quote supersedes the startup deletion and is persisted as selected."""
        old_row = {
            "instrument_id": "OPTION:SELECTED", "instrument_type": "OPTION", "symbol": "NIFTY",
            "price": 125.0, "observed_at": "2026-11-03T04:54:00Z", "source": "dhan_quote_api",
            "expiry": "2026-11-05", "strike": 23300.0, "option_type": "CE",
            "security_id": "53940", "raw_metadata": {},
        }
        mock_supabase, persisted_rows = _stateful_quote_supabase([old_row])
        svc = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        selected = {
            "expiry": "2026-11-12", "strike": 23400,
            "option_type": "PE", "security_id": "53941",
        }

        assert svc.reconcile_selected_contract(selected) is True
        assert svc.record_option_quote(selected, 151.25, "2026-11-03T04:55:01+00:00") is True
        assert svc._clear_option_pending is False

        await svc.flush(force=True)

        mock_supabase.table.return_value.delete.assert_not_called()
        selected_option = mock_supabase.rpc("get_latest_market_quotes").execute().data["selectedOption"]
        assert selected_option["securityId"] == selected["security_id"]
        assert selected_option["currentPremium"] == 151.25
        assert persisted_rows["OPTION:SELECTED"]["security_id"] == selected["security_id"]

    def test_record_option_quote_allows_different_contract_regardless_of_older_timestamp(self):
        """Verifies recording a new contract is not blocked by a previous contract's newer observation timestamp."""
        svc = QuotePersistenceService(supabase_client=MagicMock())
        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        c2 = {"expiry": "2026-11-12", "strike": 23400, "option_type": "PE", "security_id": "53941"}

        # Record contract 1 at 10:30 UTC
        assert svc.record_option_quote(c1, 125.0, "2026-11-02T10:30:00Z") is True
        # Record contract 2 at 10:25 UTC (older than contract 1, but different contract identity)
        assert svc.record_option_quote(c2, 85.0, "2026-11-02T10:25:00Z") is True
        assert svc.get_cached_option_contract()["security_id"] == "53941"

        # Recording contract 2 again with even older timestamp 10:20 UTC is rejected (same contract)
        assert svc.record_option_quote(c2, 84.0, "2026-11-02T10:20:00Z") is False

    @pytest.mark.asyncio
    async def test_flushes_are_serialized_across_database_completion(self):
        """Verifies flushes serialize across snapshot creation and database completion to prevent stale resurrection."""
        import asyncio
        import threading
        from unittest.mock import MagicMock

        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.upsert.return_value = mock_table
        mock_table.delete.return_value = mock_table
        mock_table.eq.return_value = mock_table

        svc = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        svc.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")

        op_log = []
        upsert_in_progress = threading.Event()
        allow_upsert_finish = threading.Event()

        def custom_execute():
            # Check if this execute call is for delete or upsert
            if mock_table.delete.call_count > 0 and len([op for op in op_log if op == "delete"]) < mock_table.delete.call_count:
                op_log.append("delete")
            else:
                op_log.append("upsert_start")
                upsert_in_progress.set()
                allow_upsert_finish.wait(timeout=2.0)
                op_log.append("upsert_end")
            return MagicMock(data=[])

        mock_table.execute.side_effect = custom_execute

        # 1. Start Flush 1 (upserts option)
        flush1_task = asyncio.create_task(svc.flush(force=True))

        # Wait until Flush 1's worker thread is executing upsert
        while not upsert_in_progress.is_set():
            await asyncio.sleep(0.01)

        # 2. While Flush 1 is in-flight, terminal cleanup clears option and schedules Flush 2 (delete)
        svc.clear_selected_option()
        flush2_task = asyncio.create_task(svc.flush(force=True))

        # Yield to event loop to let flush2_task attempt to run
        await asyncio.sleep(0.05)

        # Flush 2 MUST NOT have executed delete yet because flush 1 holds the flush lock!
        assert "delete" not in op_log

        # 3. Release Flush 1 upsert
        allow_upsert_finish.set()
        await flush1_task
        await flush2_task

        # Flush 2 must have executed AFTER Flush 1
        assert "upsert_end" in op_log
        assert "delete" in op_log
        assert op_log.index("upsert_end") < op_log.index("delete")
        assert svc.get_cached_option_contract() is None

    @pytest.mark.asyncio
    async def test_inflight_flush_drops_option_if_cleared_before_persist(self):
        """Verifies if option is cleared while flush is queuing, it drops OPTION:SELECTED from upsert."""
        from unittest.mock import MagicMock

        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.upsert.return_value = mock_table
        mock_table.delete.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.execute.return_value = MagicMock(data=[])

        svc = QuotePersistenceService(supabase_client=mock_supabase, throttle_seconds=0.0)
        c1 = {"expiry": "2026-11-05", "strike": 23300, "option_type": "CE", "security_id": "53940"}
        svc.record_spot_quote(23300.0, "2026-11-02T05:00:00Z")
        svc.record_option_quote(c1, 125.0, "2026-11-02T05:00:00Z")

        # Clear option
        svc.clear_selected_option()
        await svc.flush(force=True)

        # Verify no call to upsert with OPTION:SELECTED occurred
        for call in mock_table.upsert.call_args_list:
            row = call[0][0]
            assert row["instrument_id"] != "OPTION:SELECTED"
        mock_table.delete.assert_called_with()


