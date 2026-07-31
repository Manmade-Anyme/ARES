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
import json
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from supabase import create_client

# Trades in these states have not resolved, so they carry no label yet.
_OPEN_STATES = {"OPEN", None, ""}

# Sub-second in practice; 120s is a wide guard that still cannot collide because
# a given setup_type does not fire twice inside two minutes (engine cooldown).
_MATCH_TOLERANCE_SECONDS = 120

# Trade entries are written a beat after the signal, and some ares_signals rows
# carry naive-IST timestamps (TASK-172), so orphan matching gets a wider window
# than the ml_collection match. Observed deltas: 0-64s, median 0.
_ORPHAN_TOLERANCE_SECONDS = 180

# structure_features wrote this literal whenever a value was unknown. Exact
# equality only — a real distance landing on precisely 100.0 does not happen with
# prices like 24002.35, and a near-100 value is a genuine reading.
_SENTINEL_VALUE = 100.0
_SENTINEL_FIELDS = (
    "dist_to_nearest_resistance",
    "dist_to_nearest_support",
    "dist_to_pdh",
    "dist_to_pdl",
)


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
    by_id = {str(s["id"]): s for s in signals}

    def _corroborated(row: Dict[str, Any]) -> bool:
        """Is this row's existing signal_id actually the right signal?

        Membership in the id set is NOT sufficient. Legacy codes are 4-digit
        zero-padded strings, so once ares_signals.id passes 999 a stale code like
        "1234" string-matches a real id exactly and the row would be skipped as
        already-valid while pointing at an unrelated signal — which phase 2 would
        then label with another trade's outcome. Corroborate with the same
        (time, setup) evidence used to repair, so a coincidental numeric match
        cannot pass.
        """
        s = by_id.get(str(row["signal_id"]))
        if s is None:
            return False
        rt, st = _parse_ts(row.get("created_at")), _parse_ts(s.get("created_at"))
        if not (rt and st):
            return False
        if _normalise_setup(row.get("signal_setup_type")) != _normalise_setup(s.get("setup_type")):
            return False
        return abs((st - rt).total_seconds()) <= _MATCH_TOLERANCE_SECONDS

    candidates = [r for r in rows if r.get("signal_id") is not None]
    already = [r for r in candidates if _corroborated(r)]
    todo = [r for r in candidates if not _corroborated(r)]

    print(f"  signal-bearing rows      : {len(candidates)}")
    print(f"  already correct          : {len(already)}")
    print(f"  need repair              : {len(todo)}")

    by_setup: Dict[str, List[Dict[str, Any]]] = {}
    for s in signals:
        by_setup.setdefault(_normalise_setup(s.get("setup_type")), []).append(s)

    # A signal fires once, so it must be claimed by at most one row. Without this
    # two rows inside the tolerance window of the same signal both take its id,
    # and phase 2 — which updates by signal_id — writes one trade's outcome onto
    # both. Consecutive same-setup signals are real here (TASK-185 recorded three
    # exhaustion entries in three consecutive minutes), so this is reachable.
    claimed = {str(r["signal_id"]) for r in already}
    fixed = 0
    unmatched: List[int] = []
    contended: List[int] = []

    for r in sorted(todo, key=lambda x: x.get("created_at") or ""):
        rt = _parse_ts(r.get("created_at"))
        setup = _normalise_setup(r.get("signal_setup_type"))
        best, best_delta = None, None
        for s in by_setup.get(setup, []):
            if str(s["id"]) in claimed:
                continue
            st = _parse_ts(s.get("created_at"))
            if not (rt and st):
                continue
            d = abs((st - rt).total_seconds())
            if best_delta is None or d < best_delta:
                best, best_delta = s, d
        if best is None or best_delta is None or best_delta > _MATCH_TOLERANCE_SECONDS:
            # Distinguish "no signal at all" from "the only candidate was taken".
            if any(str(s["id"]) in claimed for s in by_setup.get(setup, [])):
                contended.append(r["id"])
            else:
                unmatched.append(r["id"])
            continue
        claimed.add(str(best["id"]))
        if apply:
            sb.table("ml_collection").update(
                {"signal_id": str(best["id"])}
            ).eq("id", r["id"]).execute()
        fixed += 1

    print(f"  matched -> repairable    : {fixed}")
    print(f"  unmatchable (left as-is) : {len(unmatched)}")
    if contended:
        print(f"  CONTENDED (left as-is)   : {len(contended)}  rows={contended[:10]}")
        print(f"    nearest signal was already claimed — inspect before trusting these")
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
    if not apply and orphans:
        # Phase 2 wrote nothing in dry run, so this still counts orphans it would
        # have recovered. Say so rather than let the figure read as final.
        print(f"    NOTE dry run: phase 2's recoveries are not reflected above."
              f" Under --apply this number falls and 'attributable' rises.")

    # One signal must map to one trade. If two closed trades share a signal_id the
    # second .eq() update overwrites the first, and which one survives depends on
    # pagination order — arbitrary rather than wrong-but-explainable. Report and
    # skip instead of writing a label that cannot be trusted.
    by_signal: Dict[str, List[Dict[str, Any]]] = {}
    for t in closed:
        by_signal.setdefault(str(t["signal_id"]), []).append(t)

    ambiguous = {k: v for k, v in by_signal.items() if len(v) > 1}
    if ambiguous:
        print(f"  AMBIGUOUS (skipped)      : {len(ambiguous)} signal(s) with >1 closed trade")
        for sid, ts in list(ambiguous.items())[:10]:
            print(f"    signal {sid}: trades {[t['id'] for t in ts]}")

    rows_written = 0
    trades_applied = 0
    for sid, ts in by_signal.items():
        if len(ts) > 1:
            continue
        t = ts[0]
        payload = {
            "trade_id": t["id"],
            "trade_outcome": t["result_state"],
            "trade_pnl": t.get("pnl_points"),
        }
        trades_applied += 1
        if apply:
            resp = sb.table("ml_collection").update(payload).eq("signal_id", sid).execute()
            # Count ROWS touched, not trades iterated — a trade whose signal has no
            # ml_collection row writes nothing, and the two numbers diverge.
            rows_written += len(getattr(resp, "data", None) or [])

    if apply:
        print(f"  trades applied           : {trades_applied}")
        print(f"  ml_collection rows written: {rows_written}")
    else:
        print(f"  trades attributable      : {trades_applied}  (row count unknown until --apply)")
    return rows_written if apply else trades_applied


def repair_orphan_trades(sb, apply: bool) -> int:
    """Phase 2 — recover ``trade_analytics.signal_id`` where it was never written.

    36 trades carry no signal_id, so their SL/targets cannot be read back from
    ares_signals and they can never be labelled. They are not junk: they hold
    full entry/exit data and +728.2 net points, 8 are still OPEN, and 17 are
    OI_WALL_REJECTION — the detector with the thinnest joinable sample. Deleting
    them would shrink exactly the dataset the SL/target work needs.

    Matched on (entry_timestamp, setup_type) against ares_signals, the same
    evidence phase 1 uses. Observed deltas are 0-64s, median 0.

    Runs BEFORE the label back-fill so newly linked closed trades get labelled in
    the same pass.
    """
    trades = _page(sb, "trade_analytics", "id,signal_id,setup_type,entry_timestamp,result_state")
    signals = _page(sb, "ares_signals", "id,setup_type,timestamp,created_at")

    orphans = [t for t in trades if t.get("signal_id") is None]
    print(f"  trades without a signal_id: {len(orphans)}")
    if not orphans:
        return 0

    by_setup: Dict[str, List[Dict[str, Any]]] = {}
    for s in signals:
        by_setup.setdefault(_normalise_setup(s.get("setup_type")), []).append(s)

    # A signal already attributed to another trade must not be stolen.
    claimed = {str(t["signal_id"]) for t in trades if t.get("signal_id") is not None}

    fixed = 0
    unmatched: List[str] = []
    for t in sorted(orphans, key=lambda x: x.get("entry_timestamp") or ""):
        et = _parse_ts(t.get("entry_timestamp"))
        setup = _normalise_setup(t.get("setup_type"))
        best, best_delta = None, None
        for s in by_setup.get(setup, []):
            if str(s["id"]) in claimed:
                continue
            # ares_signals.timestamp is the candle time, created_at the write
            # time; TASK-172 made some rows naive-IST, so take whichever is nearer
            # rather than trusting one column.
            for cand in (_parse_ts(s.get("timestamp")), _parse_ts(s.get("created_at"))):
                if not (cand and et):
                    continue
                d = abs((cand - et).total_seconds())
                if best_delta is None or d < best_delta:
                    best, best_delta = s, d
        if best is None or best_delta is None or best_delta > _ORPHAN_TOLERANCE_SECONDS:
            unmatched.append(t["id"])
            continue
        claimed.add(str(best["id"]))
        if apply:
            sb.table("trade_analytics").update(
                {"signal_id": best["id"]}
            ).eq("id", t["id"]).execute()
        fixed += 1

    print(f"  recovered                 : {fixed}")
    print(f"  still unattributable      : {len(unmatched)}")
    return fixed


def repair_structure_sentinel(sb, apply: bool) -> int:
    """Phase 4 — null the literal 100.0 sentinel in structure_features.

    5,226 rows (57%) carry it. NOT ONE has all four fields poisoned: every row
    has 1-3 bad fields and 29-32 good ones, so deleting rows would destroy far
    more than it removed. The fields are nulled in place instead; the rows, and
    every other feature on them, survive untouched.

    Exact float equality only. A computed distance from prices like 24002.35
    landing on precisely 100.0 is effectively impossible, so `== 100.0` is the
    marker and a near-100 value is left alone as a genuine reading.
    """
    rows = _page(sb, "ml_collection", "id,structure_features")
    dirty = []
    for r in rows:
        feats = r.get("structure_features")
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except (ValueError, TypeError):
                continue
        if not isinstance(feats, dict):
            continue
        hits = [k for k in _SENTINEL_FIELDS if feats.get(k) == _SENTINEL_VALUE]
        if hits:
            dirty.append((r["id"], feats, hits))

    field_counts: Dict[str, int] = {}
    for _, _, hits in dirty:
        for k in hits:
            field_counts[k] = field_counts.get(k, 0) + 1

    print(f"  rows carrying the sentinel: {len(dirty)} of {len(rows)}")
    for k in _SENTINEL_FIELDS:
        if field_counts.get(k):
            print(f"    {k:30} {field_counts[k]}")

    if not apply:
        return len(dirty)

    for i, (rid, feats, hits) in enumerate(dirty, 1):
        for k in hits:
            feats[k] = None
        sb.table("ml_collection").update(
            {"structure_features": json.dumps(feats)}
        ).eq("id", rid).execute()
        if i % 500 == 0:
            print(f"    ... {i}/{len(dirty)}")
    print(f"  fields nulled in place    : {sum(field_counts.values())} across {len(dirty)} rows")
    return len(dirty)


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

    print("Phase 1 — rebuild the ml_collection join key")
    repair_join_key(sb, args.apply)

    # Before phase 3: a trade recovered here becomes labellable in the same run.
    print("\nPhase 2 — recover orphaned trade_analytics.signal_id")
    repair_orphan_trades(sb, args.apply)

    print("\nPhase 3 — back-fill outcome labels")
    backfill_labels(sb, args.apply)

    print("\nPhase 4 — null the structure_features sentinel")
    repair_structure_sentinel(sb, args.apply)

    if not args.apply:
        print("\nNothing was written. Re-run with --apply to perform the repair.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
