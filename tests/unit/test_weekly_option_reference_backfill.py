from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from oi_watch_context import IST
from scripts.backfill_weekly_option_reference import DhanHistory, ExpiredContractPinUnavailable


def _epoch(value):
    return int(value.timestamp())


def test_active_option_mark_uses_exact_pinned_security_minute():
    event = datetime(2026, 10, 28, 10, 57, tzinfo=IST)
    client = Mock()
    client.intraday_minute_data.return_value = {
        "status": "success",
        "data": {
            "start_Time": [_epoch(event)], "open": [216], "high": [219],
            "low": [215], "close": [217.65], "volume": [1000],
        },
    }
    history = DhanHistory(client, now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    contract = {"security_id": "53749", "exchange_segment": "NSE_FNO",
                "expiry_date": "2026-11-03", "strike": 22600,
                "option_type": "PE", "lot_size": 65}

    mark = history.mark(contract, event)

    client.intraday_minute_data.assert_called_once_with(
        53749, "NSE_FNO", "OPTIDX", "2026-10-28", "2026-10-28", interval=1,
    )
    client.expired_options_data.assert_not_called()
    assert mark["price"] == 217.65
    assert mark["observed_at"] == "2026-10-28T10:58:00+05:30"
    assert mark["source"] == "completed_1m_close"
    assert mark["approximate"] is True
    assert mark["provenance"]["requested_security_id"] == "53749"


def test_expired_contract_is_not_guessed_from_next_expiry_rolling_data():
    client = Mock()
    history = DhanHistory(client, now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    contract = {"security_id": "51329", "exchange_segment": "NSE_FNO",
                "expiry_date": "2026-10-27", "strike": 22450,
                "option_type": "PE", "lot_size": 65}

    with pytest.raises(ExpiredContractPinUnavailable, match="code=0.*code=1 is next expiry"):
        history.mark(contract, datetime(2026, 10, 26, 9, 18, tzinfo=IST))

    client.intraday_minute_data.assert_not_called()
    client.expired_options_data.assert_not_called()
