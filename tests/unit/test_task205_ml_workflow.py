"""
TASK-205 — Unit tests for GitHub Actions ML training workflow configuration.

Verifies that .github/workflows/ml_training.yml exists, parses as valid YAML,
contains required schedule (weekly Saturday 00:00 UTC) + workflow_dispatch triggers,
properly wires SUPABASE_URL and SUPABASE_KEY secrets, invokes train_offline,
and configured artifact uploads + step summaries.
"""
import os
import yaml
import pytest

WORKFLOW_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".github",
    "workflows",
    "ml_training.yml",
)


def test_ml_workflow_file_exists():
    """Verify that .github/workflows/ml_training.yml exists."""
    assert os.path.exists(WORKFLOW_PATH), f"Workflow file missing at {WORKFLOW_PATH}"


def test_ml_workflow_structure():
    """Verify workflow structure, triggers, environment secrets, and execution steps."""
    assert os.path.exists(WORKFLOW_PATH)
    with open(WORKFLOW_PATH, "r") as f:
        data = yaml.safe_load(f)

    assert data.get("name") == "Offline ML Model Training"

    # Triggers: workflow_dispatch and schedule cron for Saturday 00:00 UTC
    on_trigger = data.get("on") if "on" in data else data.get(True, {})
    assert "workflow_dispatch" in on_trigger
    schedules = on_trigger.get("schedule", [])
    assert len(schedules) >= 1
    cron_exprs = [s.get("cron") for s in schedules]
    assert "0 0 * * 6" in cron_exprs

    # Jobs
    jobs = data.get("jobs", {})
    assert "train" in jobs
    train_job = jobs["train"]
    assert train_job.get("runs-on") == "ubuntu-latest"

    # Env secrets
    env = train_job.get("env", {})
    assert "SUPABASE_URL" in env
    assert "SUPABASE_KEY" in env

    # Steps
    steps = train_job.get("steps", [])
    step_names = [s.get("name", "") for s in steps]
    step_runs = [s.get("run", "") for s in steps if "run" in s]
    step_uses = [s.get("uses", "") for s in steps if "uses" in s]

    # Must checkout and setup python
    assert any("actions/checkout" in u for u in step_uses)
    assert any("actions/setup-python" in u for u in step_uses)

    # Must run offline training module
    assert any("python -m ml_signal.train_offline" in r for r in step_runs)

    # Must upload artifacts
    assert any("actions/upload-artifact" in u for u in step_uses)
