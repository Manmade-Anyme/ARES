"""Native concurrent PostgreSQL checks inside CI's isolated disposable service.

Uses only stdlib and docker exec; cannot consume application DB credentials.
PGlite checks lifecycle outcomes separately but cannot provide multiple sessions.
"""
import argparse
from pathlib import Path
import re
import select
import subprocess
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    container = parser.parse_args().container
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        parser.error("expected disposable CI service container ID")
    prefix = ["docker", "exec", "-i"]
    psql = ["psql", "-U", "postgres", "-d", "task211_watch_test", "-qAt", "-v", "ON_ERROR_STOP=1"]
    checks = 0

    def command(name):
        return prefix + ["-e", f"PGAPPNAME={name}", container] + psql

    def query(sql):
        return subprocess.run(command("task211-inspector"), input=sql, text=True,
                              capture_output=True, check=True, timeout=10).stdout.strip()

    def check(condition, message):
        nonlocal checks
        assert condition, message
        checks += 1

    def session(sql):
        proc = subprocess.Popen(command("task211-lock-holder"), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.stdin.write("BEGIN;\nDO $$ BEGIN " + sql + " END $$;\n\\echo READY\n")
        proc.stdin.flush()
        if not select.select([proc.stdout], [], [], 10)[0]:
            proc.kill()
            raise AssertionError("lock holder never became ready")
        line = proc.stdout.readline().strip()
        if line != "READY":
            proc.kill()
            raise AssertionError(f"unexpected lock-holder output: {line}")
        return proc

    def blocked(sql, name):
        proc = subprocess.Popen(command(name), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.stdin.write(sql + "\n")
        proc.stdin.close()
        proc.stdin = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = query(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{name}' AND wait_event_type='Lock';")
            if waiting == "1":
                return proc
            if proc.poll() is not None:
                break
            time.sleep(.02)
        proc.kill()
        raise AssertionError(f"{name} did not wait for the generation lock")

    def release(holder, sql=""):
        holder.stdin.write(sql + "\nCOMMIT;\n\\q\n")
        holder.stdin.flush()
        holder.stdin.close()
        holder.stdin = None
        output, error = holder.communicate(timeout=10)
        check(holder.returncode == 0, f"holder failed: {error}")

    def result(proc):
        output, error = proc.communicate(timeout=10)
        check(proc.returncode == 0, f"blocked request failed: {error}")
        return output.strip()

    migration = Path(__file__).resolve().parents[2] / "migrations/2026-10-08-task211-oi-watch-jev.sql"
    query("CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;" + migration.read_text())
    query("""CREATE OR REPLACE FUNCTION oi_watch_in_session(p_now timestamptz) RETURNS boolean
        LANGUAGE sql AS $$ SELECT true $$;
        CREATE OR REPLACE FUNCTION oi_watch_session_end(p_now timestamptz) RETURNS timestamptz
        LANGUAGE sql AS $$ SELECT p_now+interval '1 hour' $$;""")

    def recover(run, minute):
        return f"SELECT restart_oi_watches('{run}','2026-10-09T03:{minute:02d}:00Z');"

    def enqueue(run, event):
        return f"enqueue_oi_watch('{event}','{run}',clock_timestamp(),'{{}}','{{}}','{{}}',50,60)"

    old, new, event = str(uuid4()), str(uuid4()), str(uuid4())
    query(recover(old, 0))
    holder = session(f"PERFORM * FROM {enqueue(old, event)};")
    pending = None
    try:
        pending = blocked(recover(new, 1), "task211-successor")
        check(query(f"SELECT producer_run_id FROM oi_watch_producer_state;") == old,
              "successor must wait until old enqueue transaction commits")
        release(holder)
        result(pending)
        check(query(f"SELECT lifecycle FROM oi_watch_predictions WHERE event_id='{event}';") == "CANCELED",
              "successor must cancel insert committed immediately before activation")
        check(query(f"SELECT count(*) FROM {enqueue(old, str(uuid4()))};") == "0",
              "old insert arriving after activation must be rejected")
    finally:
        for proc in (holder, pending):
            if proc is not None and proc.poll() is None:
                proc.kill()

    # Hold only the singleton lock, then switch generations while a claim waits.
    # Without the shared run lock, the claim would complete before activation.
    for index, inference in enumerate((True, False), start=2):
        current, successor, event = str(uuid4()), str(uuid4()), str(uuid4())
        query(recover(current, index * 2))
        query(f"SELECT count(*) FROM {enqueue(current, event)};")
        if not inference:
            query(f"UPDATE oi_watch_predictions SET prediction_status='FAILED' WHERE event_id='{event}';")
        holder = session("PERFORM producer_run_id FROM oi_watch_producer_state WHERE singleton FOR UPDATE;")
        pending = None
        try:
            function = (f"claim_oi_watch_inference('{event}','{uuid4()}')" if inference else
                        f"begin_oi_watch_delivery('{event}','{uuid4()}','{{}}',false)")
            pending = blocked(f"SELECT count(*) FROM {function};", f"task211-claim-{index}")
            release(holder, recover(successor, index * 2 + 1))
            check(result(pending) == "0", "waiting claim must recheck current generation after takeover")
        finally:
            for proc in (holder, pending):
                if proc is not None and proc.poll() is None:
                    proc.kill()
    print(f"TASK-211 overlapping PostgreSQL generations: {checks} checks passed")


if __name__ == "__main__":
    main()
