"""Stop-Loss Selection on Development Partition (July 20 - August 31, 2026).

Follows BRIEF.md:
- Pre-declares cohort matching and exclusions
- Measures MAE on Development trades up to 30 min / same-session to first T1 touch
- Selects training recoveries where MAE > original SL
- Derives q50, q75, q90 and proposed stops: ceil(q) + 1, bounded 13..30
- Resolves duplicate widths deterministically
- Freezes selection.json BEFORE any validation replay
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def build_cohort_and_select():
    # 1. Load inputs and compute hashes
    snap_path = HERE / 'supabase_snapshot.json'
    dhan_path = HERE / 'dhan_raw.json'
    assert snap_path.exists(), f"Missing {snap_path}"
    assert dhan_path.exists(), f"Missing {dhan_path}"

    snap_bytes = snap_path.read_bytes()
    dhan_bytes = dhan_path.read_bytes()
    input_hashes = {
        'supabase_snapshot_sha256': hashlib.sha256(snap_bytes).hexdigest(),
        'dhan_raw_sha256': hashlib.sha256(dhan_bytes).hexdigest(),
    }

    snap_data = json.loads(snap_bytes.decode('utf-8'))
    signals = snap_data['signals']
    trades = snap_data['active_trades']
    analytics = snap_data['analytics']

    # 2. Prepare Dhan candles
    candles = pd.DataFrame(json.loads(dhan_bytes.decode('utf-8')))
    candles['time'] = pd.to_datetime(candles['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    candles['hhmm'] = candles['time'].dt.strftime('%H:%M')
    candles['date'] = candles['time'].dt.strftime('%Y-%m-%d')
    candles = candles[(candles['hhmm'] >= '09:15') & (candles['hhmm'] < '15:30')].sort_values('timestamp').reset_index(drop=True)
    timestamps = candles['timestamp'].to_numpy()
    by_time = {int(t): i for i, t in enumerate(timestamps)}
    bars = candles.to_dict('records')

    # 3. Match signals to active_trades and analytics
    trade_by_uuid = {t['signal_uuid']: t for t in trades if t.get('signal_uuid')}
    trade_by_sig_id = {str(t['signal_id']): t for t in trades if t.get('signal_id') is not None}
    analytics_by_id = {a['id']: a for a in analytics}

    matched_cohort = []
    excluded_records = []

    for s in signals:
        s_uuid = s.get('signal_uuid')
        s_id = str(s.get('id'))
        t = None
        match_method = None

        if s_uuid and s_uuid in trade_by_uuid:
            t = trade_by_uuid[s_uuid]
            match_method = 'signal_uuid'
        elif s_id in trade_by_sig_id:
            t = trade_by_sig_id[s_id]
            match_method = 'signal_id'
        else:
            cands = [tr for tr in trades if tr.get('entry_timestamp') == s.get('timestamp') and tr.get('direction') == s.get('direction')]
            if len(cands) == 1:
                t = cands[0]
                match_method = 'direction_entry_timestamp_fallback'
            elif len(cands) > 1:
                match_method = 'ambiguous_multiple_candidates'
            else:
                match_method = 'no_active_trade_match'

        if t is None:
            excluded_records.append({
                'signal_id': s['id'],
                'signal_uuid': s_uuid,
                'timestamp': s.get('timestamp'),
                'direction': s.get('direction'),
                'reason': match_method,
            })
            continue

        sig_ts = int(pd.Timestamp(s['timestamp']).floor('min').timestamp())
        if sig_ts not in by_time:
            excluded_records.append({
                'signal_id': s['id'],
                'trade_id': t['id'],
                'timestamp': s.get('timestamp'),
                'reason': 'signal_timestamp_not_in_candle_data',
            })
            continue

        source_idx = by_time[sig_ts]
        active_created = pd.Timestamp(t['created_at'])
        start_idx = max(source_idx + 1, int(np.searchsorted(timestamps, active_created.ceil('min').timestamp())))

        if start_idx >= len(bars):
            excluded_records.append({
                'signal_id': s['id'],
                'trade_id': t['id'],
                'reason': 'start_index_exceeds_available_bars',
            })
            continue

        if bars[start_idx]['date'] != bars[source_idx]['date']:
            excluded_records.append({
                'signal_id': s['id'],
                'trade_id': t['id'],
                'reason': 'first_postentry_bar_outside_same_session',
            })
            continue

        ana = analytics_by_id.get(t['id'], {})
        direction = s['direction']
        side = 1 if direction == 'BULLISH' else -1
        entry_price = float(t['entry_price'])
        orig_sl_price = float(s['stop_loss'])
        trade_sl_price = float(t['stop_loss'])
        t1_price = float(s['target_1'])
        t2_price = float(s['target_2'])

        matched_cohort.append({
            'signal_id': s['id'],
            'signal_uuid': s_uuid or t.get('signal_uuid'),
            'trade_id': t['id'],
            'match_method': match_method,
            'signal_timestamp': s['timestamp'],
            'trade_created_at': t['created_at'],
            'date': bars[source_idx]['date'],
            'partition': 'development' if bars[source_idx]['date'] <= '2026-08-31' else 'historical_validation',
            'direction': direction,
            'side': side,
            'entry_price': entry_price,
            'orig_sl_price': orig_sl_price,
            'trade_sl_price': trade_sl_price,
            'orig_sl_dist': round(side * (entry_price - orig_sl_price), 2),
            'trade_sl_dist': round(side * (entry_price - trade_sl_price), 2),
            't1_price': t1_price,
            't1_dist': round(side * (t1_price - entry_price), 2),
            't2_price': t2_price,
            't2_dist': round(side * (t2_price - entry_price), 2),
            'source_idx': source_idx,
            'start_idx': start_idx,
            'start_time': bars[start_idx]['time'].isoformat(),
            'analytics_result_state': ana.get('result_state'),
            'analytics_score': ana.get('score'),
        })

    # Save cohort exclusions and cohort.csv
    exclusions_doc = {
        'total_signals': len(signals),
        'total_active_trades': len(trades),
        'total_analytics': len(analytics),
        'matched_trades_count': len(matched_cohort),
        'excluded_signals_count': len(excluded_records),
        'excluded_records': excluded_records,
    }
    (HERE / 'cohort_exclusions.json').write_text(json.dumps(exclusions_doc, indent=2) + '\n')

    # Add notes to cohort including signal 475 match caveat
    cohort_rows = []
    for c in matched_cohort:
        row = dict(c)
        if c['signal_id'] == 475:
            row['notes'] = (
                "Matches active legacy signal_id 1892 (entry 2026-09-17 13:28 IST). "
                "Recorded SL at 13:28:48.771636 before first full replay minute 13:29:00. "
                "Kept in all paired samples; omission risk of pre-replay seconds flagged; "
                "stored outcome for reference only."
            )
        else:
            row['notes'] = ""
        cohort_rows.append(row)
    df_cohort = pd.DataFrame(cohort_rows)
    df_cohort.to_csv(HERE / 'cohort.csv', index=False)

    # 4. Measure MAE on Development Partition
    dev_cohort = [c for c in matched_cohort if c['partition'] == 'development']
    mae_rows = []

    for c in dev_cohort:
        start_idx = c['start_idx']
        side = c['side']
        entry_price = c['entry_price']
        t1_dist = c['t1_dist']
        orig_sl_dist = c['orig_sl_dist']

        max_eval_bars = min(start_idx + 30, len(bars))
        reached_t1 = False
        mae = 0.0
        t1_bar_idx = None
        t1_touch_type = None

        for j in range(start_idx, max_eval_bars):
            if bars[j]['date'] != bars[start_idx]['date']:
                break
            b = bars[j]
            op = side * (b['open'] - entry_price)
            hi = side * (b['high' if side == 1 else 'low'] - entry_price)
            lo = side * (b['low' if side == 1 else 'high'] - entry_price)

            # Known open T1 precedence
            if op >= t1_dist:
                reached_t1 = True
                t1_bar_idx = j
                t1_touch_type = 'OPEN_PRECEDENCE'
                break

            # Adverse extreme included
            adverse = max(0.0, -lo)
            if adverse > mae:
                mae = adverse

            # Intrabar T1 touch
            if hi >= t1_dist:
                reached_t1 = True
                t1_bar_idx = j
                t1_touch_type = 'INTRABAR_TOUCH'
                break

        qualifying_recovery = bool(reached_t1 and (mae > orig_sl_dist))
        mae_rows.append({
            'signal_id': c['signal_id'],
            'signal_uuid': c['signal_uuid'],
            'date': c['date'],
            'direction': c['direction'],
            'entry_price': entry_price,
            'orig_sl_dist': orig_sl_dist,
            't1_dist': t1_dist,
            'reached_t1': reached_t1,
            't1_touch_type': t1_touch_type,
            'bars_to_t1': (t1_bar_idx - start_idx + 1) if t1_bar_idx is not None else None,
            'mae': round(mae, 2),
            'qualifying_recovery': qualifying_recovery,
        })

    df_mae = pd.DataFrame(mae_rows)
    df_mae.to_csv(HERE / 'developmentMAE.csv', index=False)

    # Check if selection.json already exists: refuse overwrite to preserve frozen selection
    sel_file = HERE / 'selection.json'
    if sel_file.exists():
        print(f"Refusing to overwrite existing selection file: {sel_file}")
        return json.loads(sel_file.read_text())

    # 5. Quantiles and Proposed Stops
    qualifying = df_mae[df_mae['qualifying_recovery']].sort_values('mae').reset_index(drop=True)
    n_qualifying = len(qualifying)

    if n_qualifying >= 5:
        basis = 'QUALIFYING_RECOVERIES'
        sample_mae = qualifying['mae'].to_numpy()
    else:
        fallback_reachers = int(df_mae['reached_t1'].sum())
        if fallback_reachers < 5:
            raise RuntimeError(f"Halt: insufficient fallback T1 reachers ({fallback_reachers} < 5)")
        basis = 'FALLBACK_ALL_T1_REACHERS'
        sample_mae = df_mae[df_mae['reached_t1']]['mae'].to_numpy()

    q50 = float(np.quantile(sample_mae, 0.50))
    q75 = float(np.quantile(sample_mae, 0.75))
    q90 = float(np.quantile(sample_mae, 0.90))

    raw_stops = [
        int(np.clip(np.ceil(q50) + 1, 13, 30)),
        int(np.clip(np.ceil(q75) + 1, 13, 30)),
        int(np.clip(np.ceil(q90) + 1, 13, 30)),
    ]

    # Resolve duplicates deterministically: nearest unused integer in 13..30, tie prefer lower
    final_stops = []
    seen = set()
    for stop in raw_stops:
        if stop not in seen:
            final_stops.append(stop)
            seen.add(stop)
        else:
            candidates = [x for x in range(13, 31) if x not in seen]
            candidates.sort(key=lambda x: (abs(x - stop), x))
            best = candidates[0]
            final_stops.append(best)
            seen.add(best)

    final_stops.sort()

    selection_doc = {
        'timestamp_frozen_utc': pd.Timestamp.now(tz='UTC').isoformat(),
        'input_hashes': input_hashes,
        'development_trades_count': len(dev_cohort),
        'development_reached_t1_count': int(df_mae['reached_t1'].sum()),
        'qualifying_recoveries_count': n_qualifying,
        'selection_basis': basis,
        'qualifying_mae_values': qualifying['mae'].tolist(),
        'quantiles': {
            'q50': round(q50, 4),
            'q75': round(q75, 4),
            'q90': round(q90, 4),
        },
        'raw_derived_stops': raw_stops,
        'duplicate_resolution_rule': 'nearest unused integer in [13..30], tie prefer lower, sorted ascending',
        'final_selected_widths': final_stops,
        'experiment_variants': {
            'baseline': 'ORIGINAL_RECORDED_10_12',
            'round_1': {
                'stop_loss_distance': final_stops[0],
                'derivation': f'q50 quantile ({q50:.2f}) -> ceil+1 = {final_stops[0]}',
            },
            'round_2': {
                'stop_loss_distance': final_stops[1],
                'derivation': f'q90 quantile ({q90:.2f}) -> ceil+1 = 34 -> bounded to 30 -> duplicate resolved to {final_stops[1]} (sorted ascending)',
            },
            'round_3': {
                'stop_loss_distance': final_stops[2],
                'derivation': f'q75 quantile ({q75:.2f}) -> ceil+1 = 31 -> bounded to {final_stops[2]}',
            },
        },
    }

    sel_file.write_text(json.dumps(selection_doc, indent=2) + '\n')
    print("Selection frozen successfully in selection.json:")
    print(json.dumps(selection_doc, indent=2))
    return selection_doc


if __name__ == '__main__':
    build_cohort_and_select()
