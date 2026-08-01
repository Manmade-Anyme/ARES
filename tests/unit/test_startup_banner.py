"""The startup banner is rendered twice — console and Discord — from one source.

These pin the two failures that shipped: the Discord copy prefixed the ML line
with "+ " while the template did too (rendering "+ + [+] ML Data Collection"),
and its hardcoded detector/session strings had drifted from the console copy,
advertising a 23:30 session and omitting Trend Continuation entirely.
"""

import asyncio
from unittest.mock import patch

import alerts
from config import SESSION_DISPLAY, detector_names


class _Resp:
    def raise_for_status(self):
        """No-op: the fake webhook always succeeds."""


class _Client:
    """Minimal stand-in for httpx.AsyncClient that captures the payload."""

    captured = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _Client.captured["payload"] = json
        return _Resp()


def _render(**kwargs):
    """Return the Discord startup message body."""
    _Client.captured.clear()
    with patch.object(alerts.settings, "discord_webhook_url", "https://example.invalid"), \
         patch.object(alerts.httpx, "AsyncClient", _Client):
        asyncio.run(alerts.send_startup_alert(24429.40, 24299.70, "NON-EXPIRY", **kwargs))
    return _Client.captured["payload"]["content"]


def test_no_line_carries_a_doubled_plus():
    """The template prefixes each line with '+ '; values must not repeat it."""
    for kwargs in ({"ml_active": True, "predictor_active": True},
                   {"ml_active": False, "predictor_active": False}):
        msg = _render(**kwargs)
        doubled = [ln for ln in msg.splitlines() if ln.startswith("+ +")]
        assert not doubled, f"doubled '+ +' with {kwargs}: {doubled}"


def test_discord_banner_matches_the_real_config():
    """Detectors and session come from config, so the copy cannot drift again."""
    msg = _render(ml_active=True, predictor_active=True)

    assert f"[+] Session      : {SESSION_DISPLAY}" in msg
    assert SESSION_DISPLAY == "09:15 to 15:30 IST"       # matches main.py's gate

    for name in detector_names():
        assert name in msg, f"detector {name!r} missing from the startup alert"


def test_training_provenance_is_read_from_the_report_not_hardcoded():
    """The served model must state which offline dataset produced it."""
    from ml_signal.predictor import SignalPredictor

    predictor = SignalPredictor()
    predictor.training_info = {
        "label_source": "real_outcomes(t1_is_win=True)",
        "n_samples": 110,
        "auc_roc": 0.381,
        "provisional": True,
        "trained_at": "2026-08-03T09:00:00+00:00",
    }
    summary = predictor.training_summary()
    assert "real_outcomes(t1_is_win=True)" in summary
    assert "n=110" in summary
    assert "AUC 0.381" in summary
    assert "PROVISIONAL" in summary          # a weak model must say so
    assert "2026-08-03" in summary


def test_missing_report_degrades_instead_of_crashing():
    """A model with no report still serves; it just cannot claim provenance."""
    from ml_signal.predictor import SignalPredictor

    predictor = SignalPredictor()
    predictor.config.model_report_path = "does/not/exist.json"
    assert predictor._load_training_report() is None
    assert predictor.training_summary() == "provenance unknown"


def test_trained_on_line_appears_only_with_a_live_model():
    msg = _render(ml_active=True, predictor_active=True, training_summary="proxy · n=609")
    assert "[+] Trained on   : proxy · n=609" in msg

    # No model loaded -> no provenance claim at all, rather than a blank line.
    off = _render(ml_active=True, predictor_active=False, training_summary="proxy · n=609")
    assert "Trained on" not in off


def test_both_ml_lines_are_reported():
    """ML Predictor status was absent from the Discord banner entirely."""
    on = _render(ml_active=True, predictor_active=True)
    assert "ML Collection: ACTIVE" in on
    assert "ML Predictor : ACTIVE" in on

    off = _render(ml_active=False, predictor_active=False)
    assert "ML Collection: inactive" in off
    assert "ML Predictor : inactive" in off
