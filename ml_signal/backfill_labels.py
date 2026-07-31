"""One-time repair + ongoing catch-up for ml_collection's join key and labels.

Two defects left every one of the 9,102 collected rows unable to train anything:

1. ``signal_id`` held a RANDOM 4-digit display code instead of ``ares_signals.id``
   (see ml_signal/collector.py). ``trade_analytics.signal_id`` holds the real id,
   so the tables never joined — 0 of 119 rows overlapped in prod.
2. ``trade_id`` / ``trade_outcome`` / ``trade_pnl`` were consequently never
   written, despite schema.sql promising "back-filled when trades close".

The collector and storage.log_exit are fixed going forward. This script repairs
the history.

Phase 1 rebuilds the join key by matching each signal-bearing ml_collection row
to its ares_signals row on (created_at within tolerance, setup_type). The clock
delta between the two writes is sub-second in practice (median 0.1s, max 0.5s
observed), so the match is unambiguous. Legacy rows stored setup_type as
``str(SetupType.X)`` -> "SetupType.X", which is normalised here.

Phase 2 writes the outcome labels from every closed trade.

Both phases are idempotent and safe to re-run: phase 1 only rewrites a signal_id
that is not already a known ares_signals id, phase 2 overwrites with the same
values. DRY RUN BY DEFAULT — pass --apply to write.

    python -m ml_signal.backfill_labels            # report only, no writes
    python -m ml_signal.backfill_labels --apply    # perform the repair
"""

import argparse
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from supabase import create_client

# Trades in these states have not resolved, so they carry no label yet.
_OPEN_STATES = {"OPEN", None, ""}

# Sub-second in practice; 120s is a wide guard that still cannot collide because
# a given setup_type does not fire twice inside two minutes (engine cooldown).
_MATCH_TOLERANCE_SECONDS = 120


def _load_env(path: str = ".env") -> Dict[str, str]:
    env = {}
    for line in open(path):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalise_setup(value: Optional[str]) -> str:
    """"SetupType.OI_WALL_REJECTION" and "OI_WALL_REJECTION" must compare equal."""
    if not value:
        return ""
    return str(value).split(".")[-1].strip().upper()


def _page(sb, table: str, cols: str, size: int = 1000) -> List[Dict[str, Any]]:
    out, start = [], 0
    while True:
        r = sb.table(table).select(cols).order("id").range(start, start + size - 1).execute()
        if not r.data:
            break
        out.extend(r.data)
        if len(r.data) < size:
            break
        start += size
    return out


def repair_join_key(sb, apply: bool) -> int:
    """Phase 1 — rebuild ml_collection.signal_id as the real ares_signals.id."""
    rows = _page(sb, "ml_collection", "id,created_at,signal_id,signal_setup_type")
    signals = _page(sb, "ares_signals", "id,created_at,setup_type")
    known = {str(s["id"]) for s in signals}

    candidates = [r for r in rows if r.get("signal_id") is not None]
    already = [r for r in candidates if str(r["signal_id"]) in known]
    todo = [r for r in candidates if str(r["signal_id"]) not in known]

    print(f"  signal-bearing rows      : {len(candidates)}")
    print(f"  already a valid id       : {len(already)}")
    print(f"  need repair              : {len(todo)}")

    by_setup: Dict[str, List[Dict[str, Any]]] = {}
    for s in signals:
        by_setup.setdefault(_normalise_setup(s.get("setup_type")), []).append(s)

    fixed = 0
    unmatched = []
    for r in todo:
        rt = _parse_ts(r.get("created_at"))
        setup = _normalise_setup(r.get("signal_setup_type"))
        best, best_delta = None, None
        for s in by_setup.get(setup, []):
            st = _parse_ts(s.get("created_at"))
            if not (rt and st):
                continue
            d = abs((st - rt).total_seconds())
            if best_delta is None or d < best_delta:
                best, best_delta = s, d
        if best is None or best_delta is None or best_delta > _MATCH_TOLERANCE_SECONDS:
            unmatched.append(r["id"])
            continue
        if apply:
            sb.table("ml_collection").update(
                {"signal_id": str(best["id"])}
            ).eq("id", r["id"]).execute()
        fixed += 1

    print(f"  matched -> repairable    : {fixed}")
    print(f"  unmatchable (left as-is) : {len(unmatched)}")
    return fixed


def backfill_labels(sb, apply: bool) -> int:
    """Phase 2 — write trade_id / trade_outcome / trade_pnl from closed trades."""
    trades = _page(sb, "trade_analytics", "id,signal_id,result_state,pnl_points")
    closed = [
        t for t in trades
        if t.get("result_state") not in _OPEN_STATES and t.get("signal_id") is not None
    ]
    orphans = [t for t in trades if t.get("signal_id") is None]

    print(f"  trades total             : {len(trades)}")
    print(f"  closed & attributable    : {len(closed)}")
    print(f"  orphaned (no signal_id)  : {len(orphans)}  <- cannot be labelled")

    written = 0
    for t in closed:
        payload = {
            "trade_id": t["id"],
            "trade_outcome": t["result_state"],
            "trade_pnl": t.get("pnl_points"),
        }
        if apply:
            sb.table("ml_collection").update(payload).eq(
                "signal_id", str(t["signal_id"])
            ).execute()
        written += 1
    print(f"  label writes             : {written}")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform the writes (default is a dry run)")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    env = _load_env(args.env)
    sb = create_client(env["SUPABASE_URL"], env["SUPABASE_KEY"])

    mode = "APPLY — WRITING TO PRODUCTION" if args.apply else "DRY RUN — no writes"
    print(f"=== ml_collection label backfill [{mode}] ===\n")

    print("Phase 1 — rebuild the join key")
    repair_join_key(sb, args.apply)

    print("\nPhase 2 — back-fill outcome labels")
    backfill_labels(sb, args.apply)

    if not args.apply:
        print("\nNothing was written. Re-run with --apply to perform the repair.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
