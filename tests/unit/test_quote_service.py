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
)


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
        upserted_rows = mock_table.upsert.call_args[0][0]
        assert len(upserted_rows) == 1
        assert upserted_rows[0]["instrument_id"] == "UNDERLYING:NIFTY"
        assert upserted_rows[0]["instrument_type"] == "UNDERLYING"
        assert upserted_rows[0]["price"] == 23300.0

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
        assert mock_table.upsert.call_args[0][0][0]["price"] == 23300.5

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
        upserted_rows = mock_table.upsert.call_args[0][0]
        assert len(upserted_rows) == 2

        underlying_row = next(r for r in upserted_rows if r["instrument_id"] == "UNDERLYING:NIFTY")
        assert underlying_row["price"] == 23300.8
        assert underlying_row["instrument_type"] == "UNDERLYING"

        option_row = next(r for r in upserted_rows if r["instrument_id"] == "OPTION:SELECTED")
        assert option_row["price"] == 125.5
        assert option_row["instrument_type"] == "OPTION"
        assert option_row["expiry"] == "2026-11-03"
        assert option_row["strike"] == 23300.0
        assert option_row["option_type"] == "CE"
        assert option_row["security_id"] == "53940"

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
