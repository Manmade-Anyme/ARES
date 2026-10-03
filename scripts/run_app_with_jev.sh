#!/bin/sh
set -u

jev_pid=""
main_pid=""

stop_children() {
  trap - INT TERM

  if [ -n "$main_pid" ]; then
    kill "$main_pid" 2>/dev/null || true
  fi
  if [ -n "$jev_pid" ]; then
    kill "$jev_pid" 2>/dev/null || true
  fi

  if [ -n "$main_pid" ]; then
    wait "$main_pid" 2>/dev/null || true
  fi
  if [ -n "$jev_pid" ]; then
    wait "$jev_pid" 2>/dev/null || true
  fi
}

trap stop_children INT TERM

echo "[runner] Starting Jev consumer alongside ARES app..."
python -m system_one.consumer &
jev_pid=$!

echo "[runner] Starting ARES trading app..."
python main.py &
main_pid=$!

while kill -0 "$main_pid" 2>/dev/null; do
  if [ -n "$jev_pid" ] && ! kill -0 "$jev_pid" 2>/dev/null; then
    wait "$jev_pid"
    status=$?
    echo "[runner] Jev consumer exited with status $status; ARES app continues."
    jev_pid=""
  fi
  sleep 2
done

wait "$main_pid"
status=$?

if [ -n "$jev_pid" ]; then
  echo "[runner] ARES app exited; stopping Jev consumer..."
  kill "$jev_pid" 2>/dev/null || true
  wait "$jev_pid" 2>/dev/null || true
fi

exit "$status"
