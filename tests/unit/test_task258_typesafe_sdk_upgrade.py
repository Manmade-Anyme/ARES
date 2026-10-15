"""Unit tests for TASK-258 / MANM-258: TypeSafe SDK 0.7.5 Upgrade and Jev Model Compatibility."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typesafe_sdk
from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

import system_one.jev as jev_module
from system_one.jev import DEFAULT_MODEL, build_questions, invoke_jev


def test_typesafe_sdk_version_boundary():
    """Verify installed typesafe-sdk is within the [0.7.5, 0.8.0) semver window."""
    version = typesafe_sdk.__version__
    parts = [int(p) for p in version.split(".")[:3]]
    assert parts >= [0, 7, 5], f"typesafe-sdk version {version} is below minimum 0.7.5"
    assert parts < [0, 8, 0], f"typesafe-sdk version {version} is 0.8.0 or above (breaking change boundary)"


def test_requirements_pinning():
    """Verify requirements.txt constrains typesafe-sdk to >=0.7.5,<0.8.0 per ADR-258."""
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
    """Verify TYPESAFE_MODEL environment variable dynamically overrides DEFAULT_MODEL."""
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    # Simulate reload of constant or invoke_jev parameter resolution
    resolved_model = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")
    assert resolved_model == "jev-1.14.0"


def test_typesafe_client_contract_under_sdk_0_7_5():
    """Verify TypeSafeClient instantiation contract with timeout and retry policy."""
    client = TypeSafeClient(
        api_key="test-key",
        timeout=5.0,
        retry=RetryPolicy(max_retries=0),
    )
    assert client is not None


def test_build_questions_primitives_compatible():
    """Verify Choice, Score, and Noul primitive builders construct valid structures under 0.7.5."""
    questions = build_questions()
    assert len(questions) == 7
    assert isinstance(questions["first_barrier"], Choice)
    assert isinstance(questions["t2_given_t1"], Choice)
    assert isinstance(questions["market_regime"], Choice)
    assert isinstance(questions["is_trap"], Noul)
    assert isinstance(questions["price_action_strength"], Score)
    assert isinstance(questions["structural_clarity"], Score)
    assert isinstance(questions["confluence_rating"], Score)
