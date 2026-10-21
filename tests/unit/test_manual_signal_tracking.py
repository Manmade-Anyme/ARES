"""Real run-loop regressions for manual signal tracking and completed ATR delivery."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import alerts
import main
from config_profiles import TuningConfig
from models import AresSignal, Direction, SetupType
from options_math import process_options_calculation
from tests.unit.test_options_math import make_row
from tests.unit.test_session_close_drain import candle, loop  # noqa: F401


def trend_signal():
    return AresSignal(
        SetupType.TREND_CONTINUATION, Direction.BEARISH, 24000,
        (23995, 24005), 24025, 23975, 23920, "MEDIUM", ["Trend resumed"],
        datetime(2026, 10, 21, 10), 24000, "PE",
    )


def manual_monitors(ctx):
    ctx.positions.add_trade = AsyncMock(return_value=("manual-trend", "BOUND"))
    ctx.positions.update_atr_trailing = AsyncMock(return_value=[])
    ctx.dispatcher.on_candles = AsyncMock()
    ctx.price.watch_candles = [{
        "timestamp":"2026-10-21T09:59:00+05:30", "open":24000,
        "high":24010, "low":23990, "close":24000, "complete":True,
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("capital", [0, 100000])
async def test_real_pricing_and_discord_keep_manual_trend_tracking_without_rr_gate(loop, monkeypatch, capital):
    signal = trend_signal()
    ctx = loop([candle(hour=10, minute=0, day=21)],
               [datetime(2026, 10, 21, 10), datetime(2026, 10, 21, 15, 31)], [signal])
    manual_monitors(ctx)
    # Extreme theta makes the projected T1 reward/risk poor. It must not hide
    # premium reference levels or prevent manual signal-reference registration.
    rows = [make_row(expiry_date="2026-10-22", pe_theta=-1000)]
    ctx.oi.fetch_chain.return_value = (None, rows)
    ctx.price.dhan.get_fund_limits.return_value = {
        "status":"success", "data":{"availableBalance":capital},
    }
    config = TuningConfig()
    async def calculate(signal, chain, client):
        await process_options_calculation(signal, chain, client, config)
    monkeypatch.setattr(main, "process_options_calculation", calculate)
    monkeypatch.setattr(main, "send_discord", alerts.send_discord)
    http = AsyncMock()
    http.post.return_value = MagicMock()
    with patch("alerts.settings.discord_webhook_url", "https://example.invalid/webhook"), \
         patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = http
        await main.run()

    ctx.errors.assert_not_awaited()
    ctx.positions.add_trade.assert_awaited_once_with(signal, 24000, atm=None)
    ctx.dispatcher.dispatch.assert_not_awaited()
    ctx.dispatcher.on_snapshot.assert_awaited_once_with(rows, 24000)
    assert signal.market_context["tracking_mode"] == "SIGNAL_REFERENCE"
    assert signal.trade_id == "manual-trend" and signal.trade_binding_status == "BOUND"
    assert signal.bracket.projected_target_1 < signal.bracket.entry_limit
    assert signal.bracket.rejection_reason == ("NO_AVAILABLE_CAPITAL" if capital == 0 else None)
    assert (signal.suggested_lots == 0) is (capital == 0)
    assert signal.bracket.is_valid is (capital > 0)
    http.post.assert_awaited_once()
    fields = {field["name"]:field["value"] for field in
              http.post.call_args.kwargs["json"]["embeds"][0]["fields"]}
    for name in ("🎯 Entry Limit", "🛑 Stop Loss", "🎯 Target 1", "🎯 Target 2", "📐 Sizing"):
        assert name in fields
    for name, value in (("🎯 Entry Limit", signal.bracket.entry_limit),
                        ("🛑 Stop Loss", signal.bracket.catastrophic_opt_sl),
                        ("🎯 Target 1", signal.bracket.target_1), ("🎯 Target 2", signal.bracket.target_2)):
        assert f"{value:.2f}" in fields[name]
    reference = " (Reference only)" if capital == 0 else ""
    assert fields["📐 Sizing"] == f"{signal.suggested_lots} Lots ({signal.suggested_lots * config.nifty_lot_size} qty){reference}"
    assert ("⚠️ Option Bracket" in fields) is (capital == 0)
    assert "INSUFFICIENT_OPTION_RR" not in str(fields)
    assert "No executable bracket" not in str(fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_contract", "invalid_geometry", "calculation_failure"])
async def test_missing_or_rejected_option_data_never_blocks_manual_trade_tracking(loop, monkeypatch, mode):
    signal = trend_signal()
    ctx = loop([candle(hour=10, minute=0, day=21)],
               [datetime(2026, 10, 21, 10), datetime(2026, 10, 21, 15, 31)], [signal])
    manual_monitors(ctx)
    rows = [] if mode == "missing_contract" else [make_row(expiry_date="2026-10-22")]
    if mode == "invalid_geometry":
        signal.stop_loss = 23990  # Invalid bearish premium projection geometry.
    ctx.oi.fetch_chain.return_value = (None, rows)
    ctx.price.dhan.get_fund_limits.return_value = {
        "status":"success", "data":{"availableBalance":100000},
    }
    async def calculate(signal, chain, client):
        if mode == "calculation_failure":
            raise RuntimeError("provider cannot calculate option data")
        await process_options_calculation(signal, chain, client, TuningConfig())
    monkeypatch.setattr(main, "process_options_calculation", calculate)
    await main.run()
    ctx.errors.assert_not_awaited()
    ctx.positions.add_trade.assert_awaited_once_with(signal, 24000, atm=None)
    ctx.dispatcher.dispatch.assert_not_awaited()
    ctx.alerts.assert_awaited_once_with(signal, 24000)
    assert signal.market_context["tracking_mode"] == "SIGNAL_REFERENCE"
    assert signal.trade_binding_status == "BOUND"
    if mode == "calculation_failure":
        assert signal.bracket is None
    else:
        assert not signal.bracket.is_valid


@pytest.mark.asyncio
@pytest.mark.parametrize("final_bar", [False, True])
async def test_completed_history_reaches_both_atr_monitors_before_chain_failure(loop, final_bar):
    if final_bar:
        ctx = loop([candle(day=21), candle(minute=29, day=21)],
                   [datetime(2026, 10, 21, 15, 29), datetime(2026, 10, 21, 15, 30)])
    else:
        ctx = loop([candle(hour=10, minute=0, day=21)],
                   [datetime(2026, 10, 21, 10), datetime(2026, 10, 21, 15, 31)])
    manual_monitors(ctx)
    call_order = []
    async def positions(candles):
        assert candles is ctx.price.watch_candles
        call_order.append("positions")
    async def paper(candles):
        assert candles is ctx.price.watch_candles
        call_order.append("paper")
    async def chain(*args, **kwargs):
        assert call_order[-2:] == ["positions", "paper"]
        call_order.append("chain")
        if not final_bar or ctx.oi.fetch_chain.await_count == 2:
            raise RuntimeError("chain unavailable")
        return None, []
    ctx.positions.update_atr_trailing.side_effect = positions
    ctx.dispatcher.on_candles.side_effect = paper
    ctx.oi.fetch_chain.side_effect = chain
    await main.run()
    assert call_order == ["positions", "paper", "chain"] * (2 if final_bar else 1)
    ctx.errors.assert_awaited_once()
    ctx.dispatcher.dispatch.assert_not_awaited()
    if final_bar:
        final = candle(minute=29, day=21)
        ctx.positions.update_trades.assert_awaited_with(
            final.close, candle_high=final.high, candle_low=final.low, candle_timestamp=final.timestamp,
        )


@pytest.mark.asyncio
async def test_manual_registration_failure_does_not_hide_signal_prices(loop):
    signal = trend_signal()
    ctx = loop([candle(hour=10, minute=0, day=21)],
               [datetime(2026, 10, 21, 10), datetime(2026, 10, 21, 15, 31)], [signal])
    manual_monitors(ctx)
    ctx.positions.add_trade.side_effect = RuntimeError('temporary registration failure')
    await main.run()
    ctx.positions.add_trade.assert_awaited_once()
    ctx.dispatcher.dispatch.assert_not_awaited()
    ctx.errors.assert_not_awaited()
    ctx.alerts.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_reference_retry_failure_does_not_interrupt_polling(failed):
    positions = MagicMock()
    positions.retry_reference_registrations = AsyncMock(side_effect=RuntimeError('db down') if failed else None)
    await main._retry_reference_entries(positions)
    positions.retry_reference_registrations.assert_awaited_once()


@pytest.mark.asyncio
async def test_reference_retries_run_on_later_polls_without_new_candles(loop):
    ctx = loop([None, None], [datetime(2026, 10, 21, 10), datetime(2026, 10, 21, 10, 1), datetime(2026, 10, 21, 15, 31)])
    ctx.positions.retry_reference_registrations = AsyncMock()
    ctx.positions.update_atr_trailing = AsyncMock(return_value=[])
    ctx.dispatcher.on_candles = AsyncMock()
    await main.run()
    assert ctx.positions.retry_reference_registrations.await_count == 2
    ctx.engine.tick.assert_not_called()
    ctx.errors.assert_not_awaited()
