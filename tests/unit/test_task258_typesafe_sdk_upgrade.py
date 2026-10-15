"""Unit tests for TASK-258: TypeSafe SDK 0.7.5 Upgrade and Jev Model."""

import importlib
from pathlib import Path

import typesafe_sdk
from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

import system_one.jev as jev_module
from system_one.jev import build_questions


def test_typesafe_sdk_version_boundary():
    """Verify installed typesafe-sdk is within [0.7.5, 0.8.0) window."""
    version = typesafe_sdk.__version__
    parts = [int(p) for p in version.split(".")[:3]]
    assert parts >= [0, 7, 5], f"Version {version} is below minimum 0.7.5"
    assert parts < [0, 8, 0], f"Version {version} is >= 0.8.0 (breaking)"


def test_requirements_pinning():
    """Verify requirements.txt constrains typesafe-sdk per ADR-258."""
    repo_root = Path(__file__).resolve().parents[2]
    req_file = repo_root / "requirements.txt"
    assert req_file.exists(), "requirements.txt not found"

    content = req_file.read_text()
    assert "typesafe-sdk>=0.7.5,<0.8.0" in content, (
        "requirements.txt must explicitly pin typesafe-sdk>=0.7.5,<0.8.0"
    )


def test_default_model_adheres_to_adr258():
    """ADR-258 decision: retain jev-1.13.0 as default production model."""
    assert jev_module.DEFAULT_MODEL == "jev-1.13.0"


def test_default_model_env_override(monkeypatch):
    """Verify TYPESAFE_MODEL dynamically overrides DEFAULT_MODEL."""
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    importlib.reload(jev_module)
    try:
        assert jev_module.DEFAULT_MODEL == "jev-1.14.0"
    finally:
        monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
        importlib.reload(jev_module)
        assert jev_module.DEFAULT_MODEL == "jev-1.13.0"


def test_typesafe_client_contract_under_sdk_0_7_5():
    """Verify TypeSafeClient instantiation contract under SDK 0.7.5."""
    client = TypeSafeClient(
        api_key="test-key",
        timeout=5.0,
        retry=RetryPolicy(max_retries=0),
    )
    assert client is not None


def test_build_questions_primitives_compatible():
    """Verify Choice, Score, and Noul construct valid structures."""
    questions = build_questions()
    assert len(questions) == 7
    assert isinstance(questions["first_barrier"], Choice)
    assert isinstance(questions["t2_given_t1"], Choice)
    assert isinstance(questions["market_regime"], Choice)
    assert isinstance(questions["is_trap"], Noul)
    assert isinstance(questions["price_action_strength"], Score)
    assert isinstance(questions["structural_clarity"], Score)
    assert isinstance(questions["confluence_rating"], Score)
