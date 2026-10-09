"""Ready-watch persistence and alert lifecycle; all services are isolated fakes."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts import build_watchlist_payload, dispatch_oi_wall_watch_alerts, send_watchlist_alert
from config import settings
from detectors.oi_wall_entry import OIWallEntryFilter
from models import Direction, OHLCVCandle, OIWallBias
from oi_watch_outbox import OIWatchOutbox, initialize_oi_watch_outbox

T0 = datetime(2026, 10, 8, 9, 17)


def candle(minutes=0, close=24075, high=24085, low=24050):
    return OHLCVCandle(T0 + timedelta(minutes=minutes), close + 10, high, low, close, 1000)


def bias(minutes=0, strike=24100, persistence=3):
    return OIWallBias(
        wall_key=f"CE:{strike}", wall_strike=strike, wall_option_type="CE",
        direction=Direction.BEARISH, trade_option_type="PE", wall_oi=6299085,
        wall_oi_change_pct=9.94, relative_percentile=90, first_seen=T0,
        last_seen=T0 + timedelta(minutes=minutes), persistence_snapshots=persistence,
        persistence_duration_seconds=120, state="PERSISTENT",
        initial_interaction_timestamp=None, initial_interaction_price=None,
        favourable_excursion_pts=0, reasons=("wall",),
    )


def ready():
    entry = OIWallEntryFilter()
    entry.update(bias(persistence=2), candle(), [])
    entry.update(bias(minutes=1), candle(1, close=24055, high=24072), [])
    assert entry.latest_watch_observation is not None
    return entry


@pytest.fixture(autouse=True)
def settings_for_watch(monkeypatch):
    monkeypatch.setenv("ARES_WATCH_RUN_ID", "bf7b7c18-c9f3-4f11-9d73-dde0619975e1")
    monkeypatch.setenv("ARES_WATCH_RUN_STARTED_AT", "2026-10-09T03:45:00+00:00")
    monkeypatch.setattr(settings, "oi_wall_enable_watchlist_alert", True)
    monkeypatch.setattr(settings, "oi_watch_jev_enabled", True)
    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.invalid/webhook")


def test_outbox_uses_launchers_shared_producer_identity(monkeypatch):
    run_id = "bf7b7c18-c9f3-4f11-9d73-dde0619975e1"
    monkeypatch.setenv("ARES_WATCH_RUN_ID", run_id)
    outbox = OIWatchOutbox()
    assert outbox.producer_run_id == run_id
    assert outbox.producer_started_at == "2026-10-09T03:45:00+00:00"


def test_standalone_outbox_creates_its_own_producer_identity(monkeypatch):
    from uuid import UUID
    monkeypatch.delenv("ARES_WATCH_RUN_ID", raising=False)
    assert UUID(OIWatchOutbox().producer_run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [None, "invalid"])
async def test_uncoordinated_app_keeps_direct_watch_fallback(monkeypatch, run_id):
    if run_id is None:
        monkeypatch.delenv("ARES_WATCH_RUN_ID")
    else:
        monkeypatch.setenv("ARES_WATCH_RUN_ID", run_id)
    with patch("oi_watch_outbox.OIWatchOutbox") as factory:
        assert await initialize_oi_watch_outbox() is None
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_coordinated_app_retains_durable_watch_path():
    outbox = MagicMock(start=AsyncMock(return_value=True))
    with patch("oi_watch_outbox.OIWatchOutbox", return_value=outbox):
        assert await initialize_oi_watch_outbox() is outbox


@pytest.mark.asyncio
@pytest.mark.parametrize("started_at", [None, "invalid", "2026-10-09T03:45:00"])
async def test_invalid_run_epoch_keeps_direct_watch_fallback(monkeypatch, started_at):
    if started_at is None:
        monkeypatch.delenv("ARES_WATCH_RUN_STARTED_AT")
    else:
        monkeypatch.setenv("ARES_WATCH_RUN_STARTED_AT", started_at)
    with patch("oi_watch_outbox.OIWatchOutbox") as factory:
        assert await initialize_oi_watch_outbox() is None
        factory.assert_not_called()


def test_first_ready_snapshot_and_uuid_survive_retries_and_suppression():
    entry = ready()
    original = entry.latest_watch_observation
    with pytest.raises(FrozenInstanceError):
        original.spot = 1
    entry.update(bias(minutes=2, persistence=5), candle(2, close=24045, high=24072), [])
    assert entry.latest_watch_observation is original
    assert original.spot == 24055
    assert original.bias.persistence_snapshots == 3
    decision = entry.update(bias(minutes=3), candle(3, close=24075, high=24088), [])
    assert decision.status == "QUALIFIED"
    entry.acknowledge(decision, "SUPPRESSED_BY_COOLDOWN")
    entry.update(bias(minutes=4), candle(4, close=24045, high=24072), [])
    assert entry.latest_watch_observation is original


@pytest.mark.parametrize("ending", ["disappearance", "breach", "replacement", "rr", "reset"])
def test_terminal_uuid_emitted_even_when_watch_not_yet_delivered(ending):
    entry = ready()
    observation = entry.latest_watch_observation
    entry.acknowledge_watch_outbox(observation.event_id)
    if ending == "disappearance":
        entry.update(None, candle(2), [])
    elif ending == "breach":
        entry.update(bias(minutes=2), candle(2, close=24101, high=24111), [])
    elif ending == "replacement":
        entry.update(bias(minutes=2, strike=24200), candle(2), [])
    elif ending == "rr":
        decision = entry.update(bias(minutes=2), candle(2, high=24088), [])
        entry.acknowledge(decision, "REJECTED_BY_RR")
    else:
        entry.reset_session(candle(2))
    event, = entry.pending_watch_lifecycle
    assert event.event_id == observation.event_id
    assert event.action == "CANCEL"
    assert event.cancellation.bias == observation.bias
    assert not entry.pending_watchlist_cancellations  # worker owns this watch


@pytest.mark.parametrize("delivered", [True, False])
def test_consume_then_final_signal_resolution(delivered):
    entry = ready()
    observation = entry.latest_watch_observation
    entry.acknowledge_watch_outbox(observation.event_id)
    decision = entry.update(bias(minutes=2), candle(2, high=24088), [])
    entry.acknowledge(decision, "EMITTED")
    entry.acknowledge_signal_alert("CE:24100", delivered, candle(2), "Final signal delivery failed")
    assert [event.action for event in entry.pending_watch_lifecycle] == [
        "CONSUME", "RESOLVE" if delivered else "CANCEL",
    ]
    assert all(event.event_id == observation.event_id for event in entry.pending_watch_lifecycle)
    entry.update(None, candle(3), [])
    assert len(entry.pending_watch_lifecycle) == 2


def test_same_strike_new_episode_gets_new_uuid_after_cancellation():
    entry = ready()
    first = entry.latest_watch_observation
    entry.acknowledge_watch_outbox(first.event_id)
    entry.update(None, candle(2), [])
    entry.update(bias(minutes=3, persistence=2), candle(3), [])
    entry.update(bias(minutes=4), candle(4, close=24055, high=24072), [])
    assert entry.latest_watch_observation.event_id != first.event_id


def test_base_embed_preserves_original_format_and_observation_time():
    timestamp = datetime(2026, 10, 8, 3, 58, 33, tzinfo=timezone.utc)
    embed = build_watchlist_payload(bias(strike=22500), 22489.75, timestamp)["embeds"][0]
    assert embed["title"] == "🛡️ 🟡 #CE:22500 SETUP WATCH: OI_WALL_RETEST_READY (BEARISH)"
    assert [field["name"] for field in embed["fields"]] == [
        "🕒 Time", "📍 Current Spot", "🛡️ Wall Barrier", "⏱️ Persistence", "💡 Trader Guidance",
    ]
    assert embed["fields"][0]["value"] == "08-Oct-2026 09:28:33 IST"
    assert embed["fields"][1]["value"] == "**22489.75**"
    assert embed["footer"]["text"] == "ARES Trading System • Watchlist Observation"


@pytest.mark.asyncio
async def test_dispatch_has_one_background_owner_and_no_webhook_io():
    entry = ready()
    original = entry.latest_watch_observation
    outbox = MagicMock()
    with patch("alerts.send_watchlist_alert", new_callable=AsyncMock) as send:
        await dispatch_oi_wall_watch_alerts(entry, 99999, outbox=outbox, candles=[candle(1)])
        await dispatch_oi_wall_watch_alerts(entry, 99999, outbox=outbox)
        send.assert_not_awaited()
    outbox.submit_watch.assert_called_once()
    args, kwargs = outbox.submit_watch.call_args
    assert args[0] is original
    assert args[1]["embeds"][0]["fields"][1]["value"] == "**24055.00**"
    assert next(f["value"] for f in args[2]["embeds"][0]["fields"] if f["name"] == "Cancelled at") == "__WORKER_TIME__"
    assert kwargs["candles"] == [candle(1)]
    entry.update(None, candle(2), [])
    await dispatch_oi_wall_watch_alerts(entry, 24075, outbox=outbox)
    outbox.submit_transition.assert_called_once()
    assert outbox.submit_transition.call_args.args[:2] == (original.event_id, "CANCEL")
    assert not entry.pending_watch_lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_direct_fallback_appends_one_unavailable_field_only_when_enabled(monkeypatch, enabled):
    monkeypatch.setattr(settings, "oi_watch_jev_enabled", enabled)
    client = AsyncMock()
    client.post.return_value = MagicMock()
    with patch("alerts.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        assert await send_watchlist_alert(bias(strike=22500), 22489.75)
    embed = client.post.call_args.kwargs["json"]["embeds"][0]
    assert len(embed["fields"]) == (6 if enabled else 5)
    if enabled:
        assert embed["fields"][-1]["name"] == "🧠 JEV Prediction"
        assert "22464.75: unavailable" in embed["fields"][-1]["value"]


class FakeClient:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first
        self.delivery_statuses = ["SENT"]
        self.read_calls = 0

    def table(self, name):
        query = MagicMock()
        query.select.return_value = query
        query.eq.return_value = query

        def execute():
            self.read_calls += 1
            status = self.delivery_statuses.pop(0) if len(self.delivery_statuses) > 1 else self.delivery_statuses[0]
            return SimpleNamespace(data=[{"delivery_status": status}])

        query.execute.side_effect = execute
        return query

    def rpc(self, name, params):
        self.calls.append((name, params))
        if name == "enqueue_oi_watch" and self.fail_first:
            self.fail_first = False
            return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(TimeoutError("ambiguous register")))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))


@pytest.mark.asyncio
async def test_writer_retries_same_frozen_register_before_terminal_event():
    client = FakeClient(fail_first=True)
    outbox = OIWatchOutbox(client_factory=lambda: client)
    assert await outbox.start()
    observation = ready().latest_watch_observation
    payload = build_watchlist_payload(observation.bias, observation.spot, observation.timestamp)
    state = {"snapshot": [1]}
    with patch("oi_watch_outbox.build_watch_context", return_value=state) as build:
        outbox.submit_watch(observation, payload, {}, candles=[], full_chain=[], levels=[], pdh=None, pdl=None, expiry_date=None)
        outbox.submit_watch(observation, {}, {}, candles=[])
        build.assert_called_once()
    state["snapshot"].append(99)
    payload["embeds"][0]["title"] = "changed"
    outbox.submit_transition(observation.event_id, "CANCEL", {})
    assert await outbox.flush(3)
    await outbox.close()
    assert [name for name, _ in client.calls] == ["restart_oi_watches", "enqueue_oi_watch", "enqueue_oi_watch", "transition_oi_watch"]
    first, retry = client.calls[1][1], client.calls[2][1]
    assert first == retry
    assert first["p_input_state"] == {"snapshot": [1]}
    assert first["p_base_payload"]["embeds"][0]["title"] != "changed"


@pytest.mark.asyncio
async def test_startup_schema_failure_and_disabled_feature_return_direct_fallback(monkeypatch):
    with patch("oi_watch_outbox.OIWatchOutbox.start", new_callable=AsyncMock, return_value=False):
        assert await initialize_oi_watch_outbox() is None
    monkeypatch.setattr(settings, "oi_watch_jev_enabled", False)
    with patch("oi_watch_outbox.OIWatchOutbox") as factory:
        assert await initialize_oi_watch_outbox() is None
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_public_dispatch_freezes_inputs_and_registers_cancel_in_order():
    """Exercise real context builder, dispatcher, UUID ownership and RPC writer."""
    client = FakeClient()
    outbox = OIWatchOutbox(client_factory=lambda: client)
    assert await outbox.start()
    entry = ready()
    observed = entry.latest_watch_observation
    rows = [candle(), candle(1, close=24055, high=24072)]
    with patch("alerts.httpx.AsyncClient") as webhook:
        await dispatch_oi_wall_watch_alerts(
            entry, 24055, outbox=outbox, candles=rows, full_chain=[],
            levels=[], pdh=None, pdl=None, expiry_date="2026-10-13",
        )
        rows[-1].close = 99999
        await dispatch_oi_wall_watch_alerts(entry, 24055, outbox=outbox)
        entry.update(None, candle(2), [])
        await dispatch_oi_wall_watch_alerts(entry, 24075, outbox=outbox)
        webhook.assert_not_called()
    assert await outbox.flush(2)
    await outbox.close()
    names = [name for name, _ in client.calls]
    assert names == ["restart_oi_watches", "enqueue_oi_watch", "transition_oi_watch"]
    registration = client.calls[1][1]
    assert registration["p_event_id"] == observed.event_id
    assert registration["p_input_state"]["watch"]["spot"] == 24055
    assert registration["p_input_state"]["watch"]["closing_threshold"] == 24030
    assert client.calls[2][1]["p_event_id"] == observed.event_id
    assert client.calls[2][1]["p_action"] == "CANCEL"


@pytest.mark.asyncio
async def test_background_startup_failure_does_not_start_writer_or_live_fallback():
    def failure():
        raise RuntimeError("migration unavailable")
    outbox = OIWatchOutbox(client_factory=failure)
    assert not await outbox.start()
    assert outbox._stop.is_set()
    assert outbox._error is not None


@pytest.mark.parametrize("name,value", [
    ("oi_watch_jev_minimum_move_points", 0),
    ("oi_watch_jev_minimum_move_points", float("nan")),
    ("oi_watch_jev_wait_seconds", -1),
    ("oi_watch_jev_wait_seconds", float("inf")),
    ("oi_watch_jev_max_age_seconds", 0),
    ("oi_watch_jev_max_age_seconds", 61),
])
def test_invalid_watch_settings_rejected(name, value):
    from dataclasses import replace
    from config_profiles import OI_WATCH_JEV_CONFIG
    with pytest.raises(ValueError):
        replace(OI_WATCH_JEV_CONFIG, **{name: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation", ["builder", "copy", "serialization"])
async def test_preparation_error_keeps_same_watch_without_inference_or_alternate_send(preparation):
    from system_one.watch_consumer import compose_watch_payload
    from system_one.watch_prediction import invoke_watch_jev
    entry = ready()
    observation = entry.latest_watch_observation
    outbox = OIWatchOutbox(client_factory=lambda: FakeClient())
    payload = build_watchlist_payload(observation.bias, observation.spot, observation.timestamp)
    state = {"some": "evidence"}
    builder = patch("oi_watch_outbox.build_watch_context", return_value=state)
    copier = patch("oi_watch_outbox.deepcopy", side_effect=RuntimeError("private diagnostic")) if preparation == "copy" else patch("oi_watch_outbox.deepcopy", wraps=__import__("copy").deepcopy)
    if preparation == "serialization":
        state["invalid"] = float("nan")
    with builder as build, copier, patch("alerts.send_watchlist_alert", new_callable=AsyncMock) as direct:
        if preparation == "builder":
            build.side_effect = ValueError("private diagnostic")
        await dispatch_oi_wall_watch_alerts(entry, observation.spot, outbox=outbox)
        direct.assert_not_awaited()
    rpc, registration = outbox._queue.get_nowait()
    assert rpc == "enqueue_oi_watch"
    assert registration["p_event_id"] == observation.event_id
    assert registration["p_base_payload"] == payload
    assert entry.latest_watch_observation is None  # queued once, never alternate send
    minimal = registration["p_input_state"]
    assert set(minimal) == {"context_version", "observed_at", "context_preparation_error"}
    assert "private diagnostic" not in str(minimal)
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk:
        with pytest.raises((KeyError, ValueError)):
            invoke_watch_jev(minimal)
        sdk.assert_not_called()
    combined = compose_watch_payload({
        "base_payload": payload, "input_state": minimal, "prediction_status": "FAILED",
    })
    fields = combined["embeds"][0]["fields"]
    assert fields[:-1] == payload["embeds"][0]["fields"]
    assert fields[-1]["name"] == "🧠 JEV Prediction"
    assert "Assessment unavailable" in fields[-1]["value"]


def test_same_bar_cancellation_uses_availability_time_after_observation():
    observed_at = datetime(2026, 10, 8, 3, 48, 40, tzinfo=timezone.utc)
    canceled_at = observed_at + timedelta(seconds=5)
    with patch("detectors.oi_wall_entry.datetime", wraps=datetime) as clock:
        clock.now.side_effect = [observed_at, canceled_at]
        entry = ready()
        observation = entry.latest_watch_observation
        entry.reset_session(candle(1))
    terminal, = entry.pending_watch_lifecycle
    assert terminal.cancellation.watch_timestamp == observed_at
    assert terminal.cancellation.timestamp == canceled_at
    assert terminal.cancellation.timestamp >= observation.timestamp


@pytest.mark.asyncio
async def test_confirmed_signal_waits_for_consume_persistence_before_discord():
    from main import _deliver_signal_alert
    from models import SetupType
    client = FakeClient()
    outbox = OIWatchOutbox(client_factory=lambda: client)
    assert await outbox.start()
    entry = ready()
    observed = entry.latest_watch_observation
    await dispatch_oi_wall_watch_alerts(
        entry, observed.spot, outbox=outbox, candles=[], full_chain=[], levels=[],
        pdh=None, pdl=None, expiry_date=None,
    )
    decision = entry.update(bias(minutes=2), candle(2, high=24088), [])
    entry.acknowledge(decision, "EMITTED")
    signal = SimpleNamespace(setup_type=SetupType.OI_WALL_REJECTION, oi_wall_context={"wall_key": "CE:24100"})

    async def confirmed(signal, spot):
        assert client.calls[-1][0] == "transition_oi_watch"
        assert client.calls[-1][1]["p_action"] == "CONSUME"
        return True

    with patch("main.send_discord", side_effect=confirmed):
        await _deliver_signal_alert(signal, 24075, True, entry, candle(2), outbox=outbox)
    assert await outbox.flush()
    await outbox.close()
    assert [params["p_action"] for name, params in client.calls if name == "transition_oi_watch"] == ["CONSUME", "RESOLVE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fence_result", [False, RuntimeError("database unavailable")])
async def test_failed_consume_fence_logs_degraded_ordering_and_preserves_confirmed_alert(fence_result, caplog):
    from main import _deliver_signal_alert
    from models import SetupType
    entry = ready()
    observation = entry.latest_watch_observation
    entry.acknowledge_watch_outbox(observation.event_id)
    decision = entry.update(bias(minutes=2), candle(2, high=24088), [])
    entry.acknowledge(decision, "EMITTED")
    outbox = MagicMock()
    outbox.flush = AsyncMock(return_value=fence_result)
    outbox.wait_for_delivery = AsyncMock(return_value=True)
    if isinstance(fence_result, Exception):
        outbox.flush.side_effect = fence_result
    signal = SimpleNamespace(setup_type=SetupType.OI_WALL_REJECTION, oi_wall_context={"wall_key": "CE:24100"})
    with patch("main.send_discord", new_callable=AsyncMock, return_value=True) as send:
        await _deliver_signal_alert(signal, 24075, True, entry, candle(2), outbox=outbox)
        send.assert_awaited_once_with(signal, 24075)
    outbox.flush.assert_awaited_once_with(5.0)
    assert "ordering degraded" in caplog.text
    assert [call.args[1] for call in outbox.submit_transition.call_args_list] == ["CONSUME", "RESOLVE"]


def test_first_ready_snapshot_contains_actual_filter_excursion_and_interaction():
    entry = OIWallEntryFilter()
    detector_bias = bias(persistence=2)
    entry.update(detector_bias, candle(low=24065), [])
    entry.update(bias(minutes=1), candle(1, close=24075, high=24085, low=24070), [])
    snapshot = entry.latest_watch_observation
    assert snapshot.bias.initial_interaction_timestamp == T0
    assert snapshot.bias.initial_interaction_price == 24075
    assert snapshot.bias.favourable_excursion_pts == 30
    assert detector_bias.initial_interaction_timestamp is None
    assert detector_bias.favourable_excursion_pts == 0
    entry.update(bias(minutes=2), candle(2, close=24045, high=24072, low=24040), [])
    assert entry.favourable_excursion_pts == 60
    assert entry.latest_watch_observation is snapshot
    assert snapshot.bias.favourable_excursion_pts == 30


@pytest.mark.asyncio
async def test_delivery_read_barrier_settles_sending_before_confirmed_alert():
    from main import _deliver_signal_alert
    from models import SetupType
    client = FakeClient()
    client.delivery_statuses = ["SENDING", "SENDING", "SENT"]
    outbox = OIWatchOutbox(client_factory=lambda: client)
    assert await outbox.start()
    entry = ready()
    observation = entry.latest_watch_observation
    entry.acknowledge_watch_outbox(observation.event_id)
    decision = entry.update(bias(minutes=2), candle(2, high=24088), [])
    entry.acknowledge(decision, "EMITTED")
    signal = SimpleNamespace(setup_type=SetupType.OI_WALL_REJECTION, oi_wall_context={"wall_key": "CE:24100"})

    async def confirmed(signal, spot):
        assert client.read_calls == 3
        return True

    with patch("main.send_discord", side_effect=confirmed):
        await _deliver_signal_alert(signal, 24075, True, entry, candle(2), outbox=outbox)
    assert await outbox.flush()
    await outbox.close()
    resolve = client.calls[-1][1]
    assert resolve["p_action"] == "RESOLVE"
    payload = resolve["p_cancellation_payload"]
    assert next(field["value"] for field in payload["embeds"][0]["fields"] if field["name"] == "Reason") == "Watch superseded by confirmed signal"


@pytest.mark.asyncio
async def test_delivery_read_barrier_has_bounded_timeout_and_no_inference_wait():
    client = FakeClient()
    client.delivery_statuses = ["SENDING"]
    outbox = OIWatchOutbox(client_factory=lambda: client)
    assert await outbox.start()
    assert not await outbox.wait_for_delivery("event", timeout=0.05)
    client.delivery_statuses = ["PENDING"]  # inference pending never delays final signal
    assert await outbox.wait_for_delivery("event", timeout=0.2)
    await outbox.close()


@pytest.mark.asyncio
async def test_delivery_read_error_returns_false():
    client = FakeClient()
    client.table = MagicMock(side_effect=RuntimeError("database unavailable"))
    outbox = OIWatchOutbox(client_factory=lambda: client)
    assert await outbox.start()
    assert not await outbox.wait_for_delivery("event", timeout=0.2)
    await outbox.close()


@pytest.mark.asyncio
async def test_delivery_barrier_failure_still_posts_final_signal(caplog):
    from main import _deliver_signal_alert
    from models import SetupType
    entry = ready()
    observation = entry.latest_watch_observation
    entry.acknowledge_watch_outbox(observation.event_id)
    decision = entry.update(bias(minutes=2), candle(2, high=24088), [])
    entry.acknowledge(decision, "EMITTED")
    outbox = MagicMock()
    outbox.flush = AsyncMock(return_value=True)
    outbox.wait_for_delivery = AsyncMock(return_value=False)
    signal = SimpleNamespace(setup_type=SetupType.OI_WALL_REJECTION, oi_wall_context={"wall_key": "CE:24100"})
    with patch("main.send_discord", new_callable=AsyncMock, return_value=True) as send:
        await _deliver_signal_alert(signal, 24075, True, entry, candle(2), outbox=outbox)
        send.assert_awaited_once()
    outbox.wait_for_delivery.assert_awaited_once_with(observation.event_id, timeout=6.0)
    assert "delivery ordering barrier failed" in caplog.text


@pytest.mark.asyncio
async def test_final_signal_without_watch_episode_skips_both_barriers():
    from main import _deliver_signal_alert
    from models import SetupType
    entry = OIWallEntryFilter()
    outbox = MagicMock()
    outbox.flush = AsyncMock()
    outbox.wait_for_delivery = AsyncMock()
    signal = SimpleNamespace(setup_type=SetupType.OI_WALL_REJECTION, oi_wall_context={"wall_key": "CE:24100"})
    with patch("main.send_discord", new_callable=AsyncMock, return_value=True) as send:
        await _deliver_signal_alert(signal, 24075, True, entry, candle(2), outbox=outbox)
        send.assert_awaited_once()
    outbox.flush.assert_not_awaited()
    outbox.wait_for_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetcher_reuses_same_provider_response_and_refetches_completed_revision():
    from fetchers.price_fetcher import PriceFetcher
    first_sample = datetime(2026, 10, 8, 3, 47, 30, tzinfo=timezone.utc)
    second_sample = first_sample + timedelta(minutes=1)
    first = {"timestamp": ["2026-10-08 09:17:00"], "open": [24000],
             "high": [24020], "low": [23990], "close": [24010], "volume": [100]}
    second = {"timestamp": ["2026-10-08 09:17:00", "2026-10-08 09:18:00"],
              "open": [24000, 24015], "high": [24020, 24025], "low": [23990, 24010],
              "close": [24015, 24020], "volume": [120, 30]}
    with patch("fetchers.price_fetcher.dhanhq") as factory:
        fetcher = PriceFetcher()
    factory.return_value.intraday_minute_data.side_effect = [
        {"status": "success", "data": first}, {"status": "success", "data": second},
    ]
    with patch("fetchers.price_fetcher.datetime", wraps=datetime) as clock:
        clock.now.side_effect = [first_sample, first_sample, second_sample, second_sample]
        current_first = await fetcher.fetch_latest_candle()
        old_rows = fetcher.watch_candles
        assert current_first.close == 24010
        assert old_rows[0]["complete"] is False
        current_second = await fetcher.fetch_latest_candle()
    assert factory.return_value.intraday_minute_data.call_count == 2
    assert old_rows[0]["complete"] is False
    assert old_rows[0]["close"] == 24010
    assert fetcher.watch_candles[0]["complete"] is True
    assert fetcher.watch_candles[0]["close"] == 24015
    assert fetcher.watch_candles[1]["complete"] is False
    assert current_second.close == 24020


@pytest.mark.asyncio
async def test_watch_candle_preparation_failure_preserves_trading_candle():
    from fetchers.price_fetcher import PriceFetcher
    with patch("fetchers.price_fetcher.dhanhq") as factory:
        fetcher = PriceFetcher()
    factory.return_value.intraday_minute_data.return_value = {
        "status": "success", "data": {"timestamp": ["2026-10-08 09:17:00"],
        "open": [24000], "high": [24020], "low": [23990], "close": [24010], "volume": [100]},
    }
    with patch("fetchers.price_fetcher.build_watch_candles", side_effect=ValueError("bad data")):
        current = await fetcher.fetch_latest_candle()
    assert fetcher.watch_candles == []
    assert current.close == 24010
    assert factory.return_value.intraday_minute_data.call_count == 1
