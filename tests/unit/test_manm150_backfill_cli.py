import subprocess
import sys
from pathlib import Path


SCRIPT = Path("scripts/backfill_signal_uuids.py")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        check=False,
        text=True,
    )


def test_default_dry_run_fails_closed_until_audit_is_implemented():
    result = _run()

    assert result.returncode != 0
    assert "dry-run audit is not implemented" in result.stderr.lower()
    assert "Validation gate completed" not in result.stdout
    assert "Dry run completed" not in result.stdout


def test_apply_mode_fails_before_claiming_any_phase_ran():
    result = _run("--apply")

    assert result.returncode != 0
    assert "apply mode is not implemented" in result.stderr.lower()
    assert "Validation gate completed" not in result.stdout
