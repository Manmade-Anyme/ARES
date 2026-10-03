"""TASK-210 deployment lifecycle contract for Jev and the ARES app."""
import os


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLY_TOML = os.path.join(ROOT, "fly.toml")
RUNNER = os.path.join(ROOT, "scripts", "run_app_with_jev.sh")
CHECK_REGION = os.path.join(ROOT, ".github", "workflows", "check-region.yml")


def read(path):
    with open(path, "r") as handle:
        return handle.read()


def test_fly_runs_jev_inside_single_app_machine():
    fly = read(FLY_TOML)

    assert "app = 'sh scripts/run_app_with_jev.sh'" in fly
    assert "jev = 'python -m system_one.consumer'" not in fly
    assert "processes = ['jev']" not in fly
    assert "memory = '1024mb'" in fly
    assert "memory_mb = 1024" in fly
    assert "policy = 'always'" not in fly


def test_runner_starts_jev_and_stops_it_with_main():
    runner = read(RUNNER)

    assert "python -m system_one.consumer &" in runner
    assert "python main.py &" in runner
    assert "wait \"$main_pid\"" in runner
    assert "ARES app exited; stopping Jev consumer" in runner
    assert "kill \"$jev_pid\"" in runner


def test_region_diagnostic_rejects_running_separate_jev_machines():
    workflow = read(CHECK_REGION)

    assert "Separate running Jev Machine found" in workflow
    assert 'and .state != "stopped"' in workflow
    assert 'fly_process_group == "app"' in workflow
    assert 'fly_process_group == "jev"' in workflow
    assert "No Jev Machines found" not in workflow
