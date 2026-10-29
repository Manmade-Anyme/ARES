from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from execution.reference_recovery import _retry_deferred, run_reference_recovery
from fetchers.price_fetcher import PriceFetcher, ReferencePriceDeferred
from tests.unit.test_option_atr_lifecycle_comprehensive import _make_pm, _make_signal, _mock_trade_from_signal

IST = timezone(timedelta(hours=5, minutes=30))
T1 = datetime(2026, 10, 23, 10, 30, tzinfo=IST)
EXIT = datetime(2026, 10, 23, 10, 35, tzinfo=IST)


def bar(at, close):
    return {"timestamp": at.isoformat(), "sampled_at": (at + timedelta(minutes=1)).isoformat(),
            "complete": True, "open": close, "high": close + 2, "low": close - 2, "close": close}


def closed_row(trade_id="trade-1950"):
    trade = _mock_trade_from_signal(_make_signal(), trade_id=trade_id)
    trade["state"] = "CLOSED"
    opt = trade["market_context"]["option_trailing"]
    opt.update(accounting_formula_version="one_lot_reference_v1", phase="CLOSED", highest_milestone=1, revision=3)
    opt["accounting"] = {"legs": {
        "T1": {"source_minute": T1.isoformat(), "event_at": None, "spot_price": 22527.1},
        "EXIT": {"event_at": EXIT.isoformat(), "spot_price": None, "option_price": 155,
                 "option_source": "completed_5m_close"}}}
    return {"active_trade": trade, "option_trailing": opt, "result_state": "ATR_SL_HIT"}


def client():
    return SimpleNamespace(fetch_option_minutes=AsyncMock(return_value=[bar(T1, 150)]),
                           fetch_option_observation=AsyncMock(return_value=None),
                           fetch_spot_minutes=AsyncMock(return_value=[bar(EXIT - timedelta(minutes=1), 22520)]))


@pytest.mark.asyncio
async def test_recovery_restores_closed_evidence_and_submits_revisioned_correction():
    pm = _make_pm()
    row = closed_row()
    original = deepcopy(row)
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(return_value={"success": True}))
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    result = await run_reference_recovery(pm, fetcher, logger)
    assert result == {"rows_seen": 1, "corrected": 1}
    assert row == original
    args = logger.apply_option_accounting_correction.call_args.args
    assert args[0:2] == ("trade-1950", 3)
    assert args[2].startswith("reference-recovery-")
    accounting = args[3]
    assert accounting["legs"]["T1"]["option_price"] == 150
    assert accounting["legs"]["T1"]["approximate"] is True
    assert accounting["legs"]["EXIT"]["spot_price"] == 22520
    assert args[4] == accounting["spot_total_points"]
    assert args[5] == accounting["option_total_points"]
    fetcher.fetch_option_observation.assert_not_awaited()


@pytest.mark.asyncio
async def test_pages_are_read_before_correction_and_guard_exit_gets_matching_option_minute():
    pm = _make_pm()
    first, second = closed_row("one"), closed_row("two")
    for row in (first, second):
        leg = row["option_trailing"]["accounting"]["legs"]["EXIT"]
        leg.update(option_price=None, option_source=None, spot_price=22552.1,
                   event_at=EXIT.isoformat())
    pages = [first, second]
    actions = []
    def read(*, limit, offset):
        actions.append(("read", offset))
        return pages[offset:offset + limit]
    def correct(*args):
        actions.append(("correct", args[0]))
        return {"success": True}
    logger = SimpleNamespace(list_incomplete_option_accounting=read, apply_option_accounting_correction=correct)
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    fetcher.fetch_option_minutes.return_value = [bar(T1, 150), bar(EXIT, 145)]
    result = await run_reference_recovery(pm, fetcher, logger, limit=1)
    assert result["corrected"] == 2
    assert actions == [("read", 0), ("read", 1), ("read", 2), ("correct", "one"), ("correct", "two")]
    fetcher.fetch_option_minutes.assert_awaited_once()
    fetcher.fetch_spot_minutes.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_query_failure_does_not_stop_open_enrichment():
    pm = _make_pm()
    row = closed_row()
    trade = row["active_trade"]
    trade["state"] = "T1_HIT"
    trade["market_context"]["option_trailing"].update(phase="POST_T1")
    pm.active_trades = [trade]
    pm.enrich_reference_accounting = AsyncMock()
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(side_effect=RuntimeError("offline")),
                             apply_option_accounting_correction=Mock())
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    result = await run_reference_recovery(pm, fetcher, logger)
    assert result["rows_seen"] == 0
    pm.enrich_reference_accounting.assert_awaited_once()
    fetcher.fetch_option_minutes.assert_awaited_once()
    logger.apply_option_accounting_correction.assert_not_called()


@pytest.mark.asyncio
async def test_failed_fetch_and_unenrolled_rows_do_not_trigger_corrections():
    pm = _make_pm()
    row = closed_row()
    pm._reference_totals(row["active_trade"])
    legacy = closed_row("legacy")
    legacy["option_trailing"]["accounting_formula_version"] = None
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row, legacy]),
                             apply_option_accounting_correction=Mock())
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    fetcher.fetch_option_minutes.side_effect = OSError("offline")
    fetcher.fetch_spot_minutes.side_effect = OSError("offline")
    await run_reference_recovery(pm, fetcher, logger)
    logger.apply_option_accounting_correction.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("accounting_totals_present", [False, True])
async def test_complete_legs_recover_missing_totals_without_new_market_prices(accounting_totals_present):
    pm = _make_pm()
    row = closed_row()
    opt = row["option_trailing"]
    legs = opt["accounting"]["legs"]
    legs["T1"].update(option_price=150, option_source="completed_1m_close")
    legs["EXIT"].update(spot_price=22520, spot_source="completed_1m_close")
    if accounting_totals_present:
        pm._reference_totals(row["active_trade"])
    # The terminal columns still need to be repaired even when the accounting
    # JSON already contains totals.
    row.update(pnl_points=None, option_pnl_points=None)
    original = deepcopy(row)
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(return_value={"success": True}))
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    result = await run_reference_recovery(pm, fetcher, logger)
    assert result["corrected"] == 1
    args = logger.apply_option_accounting_correction.call_args.args
    assert args[4] is not None and args[5] is not None
    assert row == original
    fetcher.fetch_option_minutes.assert_not_awaited()
    fetcher.fetch_spot_minutes.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_conflict_does_not_mutate_row_and_retries_idempotently():
    pm = _make_pm()
    row = closed_row()
    original = deepcopy(row)
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(side_effect=[RuntimeError("revision conflict"),
                                                                                  {"success": True}]))
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    first = await run_reference_recovery(pm, fetcher, logger)
    second = await run_reference_recovery(pm, fetcher, logger)
    assert first["corrected"] == 0 and second["corrected"] == 1
    calls = logger.apply_option_accounting_correction.call_args_list
    assert calls[0].args == calls[1].args
    assert row == original


@pytest.mark.asyncio
async def test_bounded_cursor_advances_past_unrecoverable_rows_when_other_rows_disappear():
    pm = _make_pm()
    rows = [closed_row(str(index)) for index in range(4)]
    # This trade has no event timestamp for its missing target price, so the
    # historical source cannot recover it and it remains in the scan.
    target = rows[1]["option_trailing"]["accounting"]["legs"]["T1"]
    target.update(source_minute=None, event_at=None)
    offsets = []
    corrections = []
    def read(*, limit, offset):
        offsets.append(offset)
        return deepcopy(rows[offset:offset + limit])
    def correct(trade_id, revision, event_id, accounting, spot, option):
        corrections.append(trade_id)
        if spot is not None and option is not None:
            rows[:] = [row for row in rows if row["active_trade"]["id"] != trade_id]
        return {"success": True}
    logger = SimpleNamespace(list_incomplete_option_accounting=read, apply_option_accounting_correction=correct)
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    for _ in range(5):
        await run_reference_recovery(pm, fetcher, logger, limit=1, max_pages=1)
    assert offsets == [0, 0, 1, 1, 1]
    assert {"0", "2", "3"} <= set(corrections)
    assert [row["active_trade"]["id"] for row in rows] == ["1"]
    assert pm._reference_recovery_offset == 0


@pytest.mark.asyncio
async def test_recovery_limits_page_reads_and_resumes_next_pass():
    pm = _make_pm()
    rows = [closed_row(str(index)) for index in range(8)]
    # Suppress corrections so the changing offset is easy to inspect.
    for row in rows:
        opt = row["option_trailing"]
        opt["accounting"]["legs"]["T1"].update(source_minute=None, event_at=None)
        pm._reference_totals(row["active_trade"])
    offsets = []
    def read(*, limit, offset):
        offsets.append(offset)
        return deepcopy(rows[offset:offset + limit])
    logger = SimpleNamespace(list_incomplete_option_accounting=read,
                             apply_option_accounting_correction=Mock())
    logger.sync_option_reference_ml_label = Mock(return_value={"success": True, "matched": 1})
    fetcher = client()
    fetcher.fetch_spot_minutes.return_value = []
    await run_reference_recovery(pm, fetcher, logger, limit=2, max_pages=2)
    assert offsets == [0, 2] and pm._reference_recovery_offset == 4
    await run_reference_recovery(pm, fetcher, logger, limit=2, max_pages=2)
    assert offsets == [0, 2, 4, 6] and pm._reference_recovery_offset == 8
    await run_reference_recovery(pm, fetcher, logger, limit=2, max_pages=2)
    assert offsets[-1] == 8 and pm._reference_recovery_offset == 0


@pytest.mark.asyncio
async def test_closed_label_sync_runs_without_price_correction_and_retries_outage():
    pm = _make_pm()
    row = closed_row()
    legs = row["option_trailing"]["accounting"]["legs"]
    legs["T1"].update(option_price=150, option_source="completed_1m_close")
    legs["EXIT"].update(spot_price=22520, spot_source="completed_1m_close")
    totals = pm._reference_totals(row["active_trade"])
    row.update(pnl_points=totals["spot_total_points"], option_pnl_points=totals["option_total_points"])
    original = deepcopy(row)
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(),
                             sync_option_reference_ml_label=Mock(side_effect=[RuntimeError("offline"),
                                                                               {"success": True, "matched": 1}]))
    fetcher = client()
    await run_reference_recovery(pm, fetcher, logger)
    await run_reference_recovery(pm, fetcher, logger)
    assert logger.sync_option_reference_ml_label.call_count == 2
    logger.sync_option_reference_ml_label.assert_called_with("trade-1950")
    logger.apply_option_accounting_correction.assert_not_called()
    fetcher.fetch_option_minutes.assert_not_awaited()
    assert row == original


@pytest.mark.asyncio
async def test_label_sync_attempted_even_when_correction_conflicts():
    pm = _make_pm()
    row = closed_row()
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(side_effect=RuntimeError("conflict")),
                             sync_option_reference_ml_label=Mock(return_value={"success": True, "matched": 1}))
    await run_reference_recovery(pm, client(), logger)
    logger.sync_option_reference_ml_label.assert_called_once_with("trade-1950")


@pytest.mark.asyncio
async def test_spot_history_gets_a_turn_when_option_history_never_recovers():
    pm = _make_pm()
    row = closed_row()
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(return_value={"success": True}),
                             sync_option_reference_ml_label=Mock(return_value={"success": True, "matched": 0}))
    first_calls = []
    slot = {"used": False}
    async def option(*args):
        if slot["used"]:
            raise ReferencePriceDeferred()
        slot["used"] = True
        first_calls.append("option")
        return []
    async def spot(*args):
        if slot["used"]:
            raise ReferencePriceDeferred()
        slot["used"] = True
        first_calls.append("spot")
        return [bar(EXIT - timedelta(minutes=1), 22520)]
    fetcher = SimpleNamespace(fetch_option_minutes=option, fetch_spot_minutes=spot,
                              fetch_option_observation=AsyncMock(return_value=None))
    with patch("execution.reference_recovery.asyncio.sleep", new=AsyncMock()):
        await run_reference_recovery(pm, fetcher, logger)
        slot["used"] = False
        await run_reference_recovery(pm, fetcher, logger)
    assert first_calls == ["option", "spot"]
    assert logger.apply_option_accounting_correction.call_count >= 1
    assert logger.apply_option_accounting_correction.call_args.args[3]["legs"]["EXIT"]["spot_price"] == 22520


@pytest.mark.asyncio
async def test_recovery_retries_after_protection_cooldown_without_another_poll():
    pm = _make_pm()
    row = closed_row()
    logger = SimpleNamespace(list_incomplete_option_accounting=Mock(return_value=[row]),
                             apply_option_accounting_correction=Mock(return_value={"success": True}),
                             sync_option_reference_ml_label=Mock(return_value={"success": True, "matched": 1}))
    gate = PriceFetcher.__new__(PriceFetcher)
    await gate._protective_request(lambda: None)

    async def option(*args):
        return await gate._reference_request(lambda: [bar(T1, 150)])

    async def spot(*args):
        return await gate._reference_request(lambda: [bar(EXIT - timedelta(minutes=1), 22520)])

    fetcher = SimpleNamespace(fetch_option_minutes=option, fetch_spot_minutes=spot,
                              fetch_option_observation=AsyncMock(return_value=None))
    result = await run_reference_recovery(pm, fetcher, logger)
    assert result["corrected"] == 1
    accounting = logger.apply_option_accounting_correction.call_args.args[3]
    assert accounting["legs"]["T1"]["option_price"] == 150
    assert accounting["legs"]["EXIT"]["spot_price"] == 22520


@pytest.mark.asyncio
async def test_persistent_deferral_is_bounded():
    request = AsyncMock(side_effect=ReferencePriceDeferred())
    with patch("execution.reference_recovery.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(ReferencePriceDeferred):
            await _retry_deferred(request)
    assert request.await_count == 3
    assert sleep.await_count == 2
