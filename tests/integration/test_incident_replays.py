"""Recorded-source counterfactual replays for #1950 and #3935.

These fixtures contain one session through approximately 11:56 IST, rather than
chart-equivalent prior-session warmup or tick-level fills. Replay applies the
candidate policy to recorded index/option candles, without inserting exit bars.
An explicitly injected in-memory transaction adapter verifies commit ordering;
it does not claim PostgreSQL transaction or production deployment verification.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from options_candles import IST, aggregate_minute_candles_to_5m
from tests.unit.test_option_lifecycle_commit_boundary import make_pm

FIXTURES = Path(__file__).parents[1] / "fixtures"


def recorded_source(security_id):
    spot = json.loads((FIXTURES / "ares-20261023-atr-source.json").read_text())
    option = json.loads((FIXTURES / f"ares-20261023-option-{security_id}.json").read_text())
    # sampled_at records when the historical response was retrieved. Source
    # timestamp + one minute is the first knowable completed-candle event time.
    observed_at = min(datetime.fromisoformat(spot[-1]["sampled_at"]),
                      datetime.fromisoformat(option[-1]["sampled_at"]))
    completed_spot = [r for r in spot if r.get("complete") is True
                      and datetime.fromisoformat(r["timestamp"]) + timedelta(minutes=1) <= observed_at]
    option_bars = aggregate_minute_candles_to_5m(option, observed_at=observed_at,
                                                expected_security_id=security_id)
    return completed_spot, option_bars, observed_at


async def replay(monkeypatch, incident):
    pm, trade, sender = make_pm(monkeypatch)
    opt = trade["market_context"]["option_trailing"]
    if incident == "1950":
        trade.update(id="efbd19ae-b4d2-5e7a-a610-74251d2b3218", display_id="1950",
                     signal_uuid="3ae913f6-084d-441f-967d-1e843326f3f1",
                     setup_type="TREND_CONTINUATION", entry_timestamp="2026-10-23T10:08:38+05:30")
        security_id = "51333"
    else:
        trade.update(id="6a977c96-0648-5e06-9ea2-d6730d794929", display_id="3935",
                     signal_uuid="3781b4a9-8e32-4a3f-971a-9be8616669a4",
                     entry_price=22434.8, stop_loss=22459.8, target_1=22409.8, target_2=22354.8,
                     entry_timestamp="2026-10-23T11:05:58+05:30")
        security_id = "51331"
        opt["contract"].update(security_id=security_id, strike=22500)
        opt.update(original_option_sl=150.65, original_option_entry=157.25)
    opt["contract"]["expiry"] = "2026-10-27"
    spot, option, observed_at = recorded_source(security_id)
    fetcher = SimpleNamespace(fetch_option_candles=AsyncMock(return_value=option))
    dispatcher = SimpleNamespace(on_candles=AsyncMock())
    # Exercise the actual runtime entry point and real PositionManager merge.
    await main._monitor_completed_atr(spot, pm, dispatcher, price_fetcher=fetcher, current_time=observed_at)
    trace = []
    for row in pm.analytics.rows:
        alert = row.get("alert_snapshot")
        if alert:
            trace.append({"event": alert["update_type"], "event_time": alert["event_time"],
                          "score": row["score"], "active_state": row["active_state"],
                          "locked_option_sl": row["option_trailing"].get("locked_option_sl"),
                          "remaining_fraction": row["option_trailing"]["remaining_fraction"]})
    return pm, trade, sender, trace, observed_at


@pytest.mark.asyncio
async def test_recorded_1950_chronology_reaches_t2_without_fabricated_exit(monkeypatch, tmp_path):
    pm, trade, sender, trace, observed_at = await replay(monkeypatch, "1950")
    t1 = [row for row in trace if row["event"] == "T1_HIT"]
    t2 = [row for row in trace if row["event"] == "T2_HIT"]
    assert len(t1) == len(t2) == 1
    assert datetime.fromisoformat(t1[0]["event_time"]).astimezone(IST).strftime("%H:%M") == "10:10"  # 10:09 source bar ends 10:10 IST
    assert datetime.fromisoformat(t2[0]["event_time"]).astimezone(IST).strftime("%H:%M") == "10:16"  # 10:15 source bar ends 10:16 IST
    assert trade["state"] == "T2_REACHED"
    opt = trade["market_context"]["option_trailing"]
    assert opt["highest_milestone"] == 2
    assert opt["remaining_fraction"] == pytest.approx(.2)
    assert opt["index_guard_enabled"] is False
    assert not [row for row in trace if row["active_state"] == "CLOSED"]
    assert pm.analytics.rows[-1]["score"] == 2
    assert opt["revision"] == pm.analytics.revision
    stops = [row["locked_option_sl"] for row in trace if row["locked_option_sl"] is not None]
    assert stops == sorted(stops)
    (tmp_path / "1950-recorded-source-trace.json").write_text(json.dumps({
        "incident": "1950", "fixture_until": observed_at.isoformat(),
        "warmup": "single-session candidate; parity gate remains disabled", "events": trace}, indent=2))


@pytest.mark.asyncio
async def test_recorded_3935_correct_contract_and_initial_index_stop(monkeypatch, tmp_path):
    pm, trade, sender, trace, observed_at = await replay(monkeypatch, "3935")
    assert trade["market_context"]["option_trailing"]["contract"] == {
        "security_id": "51331", "exchange_segment": "NSE_FNO", "expiry": "2026-10-27",
        "strike": 22500, "option_type": "PE"}
    exits = [row for row in trace if row["event"] == "SL_HIT"]
    assert len(exits) == 1
    assert datetime.fromisoformat(exits[0]["event_time"]).astimezone(IST).strftime("%H:%M") == "11:10"  # 11:09 source bar ends 11:10 IST
    assert not [row for row in trace if row["event"] in {"T1_HIT", "T2_HIT"}]
    assert trade["state"] == "CLOSED"
    assert pm.analytics.rows[-1]["score"] == 0
    assert pm.analytics.rows[-1]["pnl_points"] == pytest.approx(-25)
    assert trade["market_context"]["option_trailing"]["remaining_fraction"] == 0
    (tmp_path / "3935-recorded-source-trace.json").write_text(json.dumps({
        "incident": "3935", "fixture_until": observed_at.isoformat(),
        "resolution": "completed one-minute bars; actual intrabar touch time is unknown", "events": trace}, indent=2))
