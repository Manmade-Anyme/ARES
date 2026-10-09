"""Exercise the real launcher with harmless child processes, never live services."""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from uuid import UUID


RUNNER = Path(__file__).resolve().parents[2] / "scripts/run_app_with_jev.sh"


def test_launcher_shares_a_fresh_identity_with_both_children(tmp_path):
    helper = tmp_path / "child.py"
    helper.write_text('''import json, os, pathlib, sys, time
kind = "consumer" if sys.argv[1] == "-m" else "app"
root = pathlib.Path(os.environ["WATCH_TEST_DIR"])
(root / (kind + ".json")).write_text(json.dumps([os.environ.get("ARES_WATCH_RUN_ID"), os.environ.get("ARES_WATCH_RUN_STARTED_AT")]))
deadline = time.monotonic() + 3
while not (root / ("app.json" if kind == "consumer" else "consumer.json")).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("other child never started")
    time.sleep(.01)
''')
    python = tmp_path / "python"
    python.write_text('''#!/bin/sh
if [ "$1" = "-c" ]; then
    exec "$WATCH_TEST_PYTHON" "$@"
fi
exec "$WATCH_TEST_PYTHON" "$WATCH_TEST_DIR/child.py" "$@"
''')
    python.chmod(0o755)
    inherited = "00000000-0000-0000-0000-000000000001"
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "WATCH_TEST_DIR": str(tmp_path), "WATCH_TEST_PYTHON": sys.executable,
           "ARES_WATCH_RUN_ID": inherited, "ARES_WATCH_RUN_STARTED_AT": "2000-01-01T00:00:00+00:00"}
    subprocess.run(["sh", str(RUNNER)], env=env, check=True, capture_output=True, timeout=10)
    app = json.loads((tmp_path / "app.json").read_text())
    consumer = json.loads((tmp_path / "consumer.json").read_text())
    assert app == consumer and app[0] != inherited
    assert str(UUID(app[0])) == app[0]
    assert datetime.fromisoformat(app[1]).utcoffset().total_seconds() == 0
    assert app[1] != env["ARES_WATCH_RUN_STARTED_AT"]


def test_launcher_does_not_fork_if_identity_generation_fails(tmp_path):
    python = tmp_path / "python"
    python.write_text('''#!/bin/sh
if [ "$1" = "-c" ]; then exit 7; fi
touch "$WATCH_TEST_DIR/child-started"
''')
    python.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "WATCH_TEST_DIR": str(tmp_path)}
    result = subprocess.run(["sh", str(RUNNER)], env=env, capture_output=True, timeout=10)
    assert result.returncode == 7
    assert not (tmp_path / "child-started").exists()


def test_launcher_restarts_failed_consumer_with_the_same_generation(tmp_path):
    helper = tmp_path / "child.py"
    helper.write_text('''import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["WATCH_TEST_DIR"])
metadata = [os.environ["ARES_WATCH_RUN_ID"], os.environ["ARES_WATCH_RUN_STARTED_AT"]]
if sys.argv[1] == "-m":
    prior = root / "first-consumer.json"
    if not prior.exists():
        prior.write_text(json.dumps(metadata))
        sys.exit(9)
    (root / "replacement-consumer.json").write_text(json.dumps(metadata))
    # Keep this replacement alive so the runner must stop it when the app exits.
    while True:
        time.sleep(.01)
else:
    (root / "app.json").write_text(json.dumps(metadata))
    deadline = time.monotonic() + 5
    while not (root / "replacement-consumer.json").exists():
        if time.monotonic() >= deadline:
            sys.exit(13)
        time.sleep(.01)
    (root / "app-finished").touch()
''')
    python = tmp_path / "python"
    python.write_text('''#!/bin/sh
if [ "$1" = "-c" ]; then
    exec "$WATCH_TEST_PYTHON" "$@"
fi
exec "$WATCH_TEST_PYTHON" "$WATCH_TEST_DIR/child.py" "$@"
''')
    python.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "WATCH_TEST_DIR": str(tmp_path), "WATCH_TEST_PYTHON": sys.executable}
    result = subprocess.run(["sh", str(RUNNER)], env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout.decode()
    app = json.loads((tmp_path / "app.json").read_text())
    assert json.loads((tmp_path / "first-consumer.json").read_text()) == app
    assert json.loads((tmp_path / "replacement-consumer.json").read_text()) == app
    assert (tmp_path / "app-finished").exists()
