"""Delivery boundary tests: no transport without a gate, no unsafe state changes."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from system_one import discord


class DeliveryDatabase:
    """Apply token/status filters to a single durable job, like a conditional UPDATE."""

    def __init__(self):
        self.row = {"signal_uuid": "signal-1", "alert_attempt_token": "attempt-1",
                    "alert_status": "SENDING"}
        self.updates = []
        self.fail_writes = False
        self.rpc = MagicMock()
        self.rpc.return_value.execute.return_value.data = {
            "is_fresh": True, "remaining_seconds": 20,
            "alert_attempt_token": "attempt-1", "alert_payload": {"embeds": []},
            "alert_destination": "https://discord.test/pinned",
        }

    def table(self, name):
        assert name == "llm_prediction_jobs"
        return DeliveryUpdate(self)


class DeliveryUpdate:
    def __init__(self, database):
        self.database = database
        self.filters = []

    def update(self, values):
        self.values = values
        return self

    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key) == value)
        return self

    def in_(self, key, values):
        self.filters.append(lambda row: row.get(key) in values)
        return self

    def execute(self):
        self.database.updates.append(self.values)
        if self.database.fail_writes:
            raise ConnectionError("database acknowledgment unavailable")
        if not all(check(self.database.row) for check in self.filters):
            return SimpleNamespace(data=[])
        self.database.row.update(self.values)
        return SimpleNamespace(data=[dict(self.database.row)])


@pytest.fixture
def delivery(monkeypatch):
    database = DeliveryDatabase()
    client_factory = MagicMock()
    client = client_factory.return_value.__enter__.return_value
    client.post.return_value = httpx.Response(200, json={"id": "message-1"})
    monkeypatch.setattr(discord.httpx, "Client", client_factory)
    return database, client_factory, client


def send(database, webhook="https://discord.test/configured"):
    return discord.send_jev_followup(
        database, "signal-1", "SIG-1", "BREAKOUT", "BULLISH", .6, .3, .2,
        "TRENDING", .8, 7.5, .18, "jev", 1, webhook,
    )


def rpc_response(data):
    return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))


def test_missing_webhook_never_queries_or_sends(delivery, monkeypatch):
    database, factory, _ = delivery
    monkeypatch.setattr(discord, "WEBHOOK_URL", "")
    assert send(database, webhook=None) == "NONE"
    database.rpc.assert_not_called()
    factory.assert_not_called()
    assert database.updates == []


@pytest.mark.parametrize("failure", [ConnectionError("unavailable"),
                                    ValueError("invalid remaining_seconds")])
def test_failed_database_gate_never_acquires_or_sends(delivery, failure):
    database, factory, _ = delivery
    database.rpc.side_effect = failure
    assert send(database) == "NONE"
    factory.assert_not_called()
    assert database.updates == []


@pytest.mark.parametrize("data", [None, [], {}])
def test_rejected_sending_marker_never_starts_transport(delivery, data):
    database, factory, _ = delivery
    gate = {"is_fresh": True, "remaining_seconds": 20}
    database.rpc.side_effect = [rpc_response(gate), rpc_response(data)]
    assert send(database) == "NONE"
    factory.assert_not_called()
    assert database.updates == []


def test_final_database_error_is_definite_no_send(delivery):
    database, _, client = delivery
    initial = database.rpc.return_value.execute.return_value.data
    database.rpc.side_effect = [rpc_response(initial), rpc_response(initial),
                                ConnectionError("final gate unavailable")]
    assert send(database) == "DELIVERY_FAILED"
    client.post.assert_not_called()
    assert database.row["alert_rejection_reason"] == "database_error"


def test_expired_acknowledgment_budget_prevents_transport(delivery, monkeypatch):
    database, _, client = delivery
    database.rpc.return_value.execute.return_value.data["remaining_seconds"] = .5
    monotonic = iter([10., 10.5])
    monkeypatch.setattr(discord.time, "monotonic", lambda: next(monotonic))
    assert send(database) == "SUPPRESSED_EXPIRED"
    client.post.assert_not_called()
    assert database.row["alert_status"] == "SUPPRESSED_EXPIRED"
    assert database.row["alert_rejection_reason"] == "expired"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 413, 422])
def test_definite_http_rejection_is_terminal(delivery, status):
    database, _, client = delivery
    client.post.return_value = httpx.Response(status, text="rejected")
    assert send(database) == "DELIVERY_FAILED"
    client.post.assert_called_once()
    assert database.row["alert_rejection_reason"] == f"http_{status}: rejected"


@pytest.mark.parametrize("status", [204, 301, 500, 502])
def test_unconfirmed_http_response_stays_unknown(delivery, status):
    database, _, client = delivery
    client.post.return_value = httpx.Response(status)
    assert send(database) == "DELIVERY_UNKNOWN"
    client.post.assert_called_once()
    assert database.row["alert_rejection_reason"] == f"ambiguous_http_{status}"


@pytest.mark.parametrize("backoff", [-1, "nan", "inf", "not-a-number"])
def test_invalid_rate_limit_backoff_cannot_schedule_retry(delivery, backoff):
    database, _, client = delivery
    client.post.return_value = httpx.Response(429, json={"retry_after": backoff})
    assert send(database) == "DELIVERY_UNKNOWN"
    assert database.row["alert_status"] == "DELIVERY_UNKNOWN"
    assert "alert_backoff_until" not in database.row


def test_rate_limit_header_fallback_schedules_retry(delivery):
    database, _, client = delivery
    client.post.return_value = httpx.Response(429, json={}, headers={"Retry-After": "3"})
    assert send(database) == "RETRYABLE"
    assert database.row["alert_rejection_reason"] == "rate_limited: retry_after=3"
    assert database.row["alert_backoff_until"] > database.row["updated_at"]


def test_success_uses_pinned_destination_and_payload(delivery):
    database, _, client = delivery
    assert send(database) == "SENT"
    client.post.assert_called_once_with(
        "https://discord.test/pinned", params={"wait": "true"},
        json={"embeds": []}, headers={"Content-Type": "application/json"},
    )
    assert database.row["alert_discord_message_id"] == "message-1"


def test_exhausted_success_persistence_never_reposts(delivery):
    database, _, client = delivery
    database.fail_writes = True
    assert send(database) == "DELIVERY_UNKNOWN"
    client.post.assert_called_once()
    assert len(database.updates) == 3
    assert all(update["alert_discord_message_id"] == "message-1"
               for update in database.updates)
    assert database.row["alert_status"] == "SENDING"


@pytest.mark.parametrize("outcome", ["retryable", "failed", "unknown", "suppressed"])
def test_state_persistence_failure_keeps_attempt_owned(delivery, outcome, caplog):
    database, _, client = delivery
    database.fail_writes = True
    if outcome == "retryable":
        client.post.return_value = httpx.Response(429, json={"retry_after": 1})
        expected = "RETRYABLE"
    elif outcome == "failed":
        client.post.return_value = httpx.Response(403)
        expected = "DELIVERY_FAILED"
    elif outcome == "unknown":
        client.post.return_value = httpx.Response(500)
        expected = "DELIVERY_UNKNOWN"
    else:
        database.rpc.return_value.execute.return_value.data = {
            "is_fresh": False, "reason": "expired"}
        expected = "SUPPRESSED_EXPIRED"
    assert send(database) == expected
    assert database.row["alert_status"] == "SENDING"
    assert "database acknowledgment unavailable" in caplog.text
    assert client.post.call_count == (0 if outcome == "suppressed" else 1)


@pytest.mark.parametrize("mark", [discord._mark_sent, discord._mark_failed,
                                discord._mark_unknown, discord._mark_retryable,
                                discord._mark_suppressed])
def test_old_attempt_token_cannot_overwrite_current_owner(mark):
    database = DeliveryDatabase()
    original = dict(database.row)
    if mark is discord._mark_retryable:
        mark(database, "signal-1", "obsolete-token", "old failure", 1)
    elif mark is discord._mark_suppressed:
        mark(database, "signal-1", "expired", "obsolete-token")
    else:
        result = mark(database, "signal-1", "obsolete-token", "old result")
        if mark is discord._mark_sent:
            assert result is False
    assert database.row == original


@pytest.mark.parametrize("status", ["SENT", "DELIVERY_UNKNOWN", "DELIVERY_FAILED"])
def test_suppression_cannot_erase_terminal_delivery(status):
    database = DeliveryDatabase()
    database.row["alert_status"] = status
    discord._mark_suppressed(database, "signal-1", "expired")
    assert database.row["alert_status"] == status
