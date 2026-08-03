"""
TASK-205 — Unit tests for GitHub Actions ML training workflow configuration.

Verifies that .github/workflows/ml_training.yml exists,
contains required schedule (weekly Saturday 00:00 UTC) + workflow_dispatch triggers,
properly wires SUPABASE_URL and SUPABASE_KEY secrets, invokes train_offline,
and configures artifact uploads, retention, auto-commit, and step summaries.
"""
import os

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
    """Verify workflow triggers, secrets scoping, python version, retention, and execution steps via file inspection."""
    assert os.path.exists(WORKFLOW_PATH)
    with open(WORKFLOW_PATH, "r") as f:
        content = f.read()

    assert "name: Offline ML Model Training" in content

    # Triggers: workflow_dispatch and schedule cron for Saturday 00:00 UTC
    assert "workflow_dispatch:" in content
    assert 'cron: "0 0 * * 6"' in content or "cron: '0 0 * * 6'" in content

    # Job & Runner & Permissions
    assert "runs-on: ubuntu-latest" in content
    assert "contents: write" in content

    # Env secrets scoped to training step
    assert "SUPABASE_URL: ${{ secrets.SUPABASE_URL }}" in content
    assert "SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}" in content

    # Steps & Configuration
    assert "actions/checkout@v4" in content
    assert "actions/setup-python@v5" in content
    assert 'python-version: "3.10"' in content
    assert "python -m ml_signal.train_offline" in content

    # Auto-commit & push
    assert "git commit -m" in content
    assert "git push origin main" in content

    # Artifacts & Step summary
    assert "actions/upload-artifact@v4" in content
    assert "ml_signal/models/" in content
    assert "reports/ml/" in content
    assert "retention-days: 30" in content
    assert "$GITHUB_STEP_SUMMARY" in content
