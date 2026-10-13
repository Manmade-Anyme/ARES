"""Replay and evaluation engine for Exhaustion Stop-Loss Experiments.

Evaluates 165 matched trades across:
- Baseline (original recorded SL 10/12)
- Round 1 (SL = 22, q50 derived)
- Round 2 (SL = 29, q75 derived, duplicate resolved)
- Round 3 (SL = 30, q90 derived, bounded)

Outputs variant ledgers, summaries, paired bootstrap CI (seed 42, 2000 resamples),
risk gate compliance, exposure metrics, and updates progress.json after each round.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def load_inputs():
    snap_bytes = (HERE / 'supabase_snapshot.json').read_bytes()
    sel = json.loads((HERE / 'selection.json').read_text())
    dhan_bytes = (HERE / 'dhan_raw.json').read_bytes()

    # Verify input hashes before replay
    snap_hash = hashlib.sha256(snap_bytes).hexdigest()
    dhan_hash = hashlib.sha256(dhan_bytes).hexdigest()
    expected_snap = sel['input_hashes']['supabase_snapshot_sha256']
    expected_dhan = sel['input_hashes']['dhan_raw_sha256']
    if snap_hash != expected_snap:
        raise ValueError(f"Supabase snapshot hash mismatch: {snap_hash} != {expected_snap}")
    if dhan_hash != expected_dhan:
        raise ValueError(f"Dhan raw hash mismatch: {dhan_hash} != {expected_dhan}")

    snap = json.loads(snap_bytes.decode('utf-8'))
    dhan_raw = json.loads(dhan_bytes.decode('utf-8'))

    candles = pd.DataFrame(dhan_raw)
    candles['time'] = pd.to_datetime(candles['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    candles['hhmm'] = candles['time'].dt.strftime('%H:%M')
    candles['date'] = candles['time'].dt.strftime('%Y-%m-%d')
    candles = candles[(candles['hhmm'] >= '09:15') & (candles['hhmm'] < '15:30')].sort_values('timestamp').reset_index(drop=True)
    timestamps = candles['timestamp'].to_numpy()
    by_time = {int(t): i for i, t in enumerate(timestamps)}
    bars = candles.to_dict('records')

    signals = snap['signals']
    trades = snap['active_trades']
    analytics = snap['analytics']

    trade_by_uuid = {t['signal_uuid']: t for t in trades if t.get('signal_uuid')}
    trade_by_sig_id = {str(t['signal_id']): t for t in trades if t.get('signal_id') is not None}
    analytics_by_id = {a['id']: a for a in analytics}

    cohort = []
    for s in signals:
        s_uuid = s.get('signal_uuid')
        s_id = str(s.get('id'))
        t = trade_by_uuid.get(s_uuid) or trade_by_sig_id.get(s_id)
        if not t:
            cands = [tr for tr in trades if tr.get('entry_timestamp') == s.get('timestamp') and tr.get('direction') == s.get('direction')]
            if len(cands) == 1:
                t = cands[0]
        if not t:
            continue

        sig_ts = int(pd.Timestamp(s['timestamp']).floor('min').timestamp())
        if sig_ts not in by_time:
            continue
        source_idx = by_time[sig_ts]
        active_created = pd.Timestamp(t['created_at'])
        start_idx = max(source_idx + 1, int(np.searchsorted(timestamps, active_created.ceil('min').timestamp())))
        if start_idx >= len(bars) or bars[start_idx]['date'] != bars[source_idx]['date']:
            continue

        ana = analytics_by_id.get(t['id'], {})
        direction = s['direction']
        side = 1 if direction == 'BULLISH' else -1
        entry_price = float(t['entry_price'])
        orig_sl_price = float(s['stop_loss'])
        orig_sl_dist = round(side * (entry_price - orig_sl_price), 2)
        t1_price = float(s['target_1'])
        t1_dist = round(side * (t1_price - entry_price), 2)
        t2_price = float(s['target_2'])
        t2_dist = round(side * (t2_price - entry_price), 2)

        cohort.append({
            'signal_id': s['id'],
            'signal_uuid': s_uuid or t.get('signal_uuid'),
            'trade_id': t['id'],
            'signal_timestamp': s['timestamp'],
            'trade_created_at': t['created_at'],
            'date': bars[source_idx]['date'],
            'month': bars[source_idx]['date'][:7],
            'partition': 'development' if bars[source_idx]['date'] <= '2026-08-31' else 'historical_validation',
            'direction': direction,
            'side': side,
            'entry_price': entry_price,
            'orig_sl_price': orig_sl_price,
            'orig_sl_dist': orig_sl_dist,
            't1_price': t1_price,
            't1_dist': t1_dist,
            't2_price': t2_price,
            't2_dist': t2_dist,
            'source_idx': source_idx,
            'start_idx': start_idx,
            'start_time': bars[start_idx]['time'].isoformat(),
            'open_entry_price': bars[start_idx]['open'],
            'ana_result_state': ana.get('result_state'),
            'ana_score': ana.get('score'),
        })

    return bars, cohort, sel


def score_trade(bars, start, side, entry, stop_distance, target1_dist, target2_dist, strict=True, same_day=False, optimistic=False):
    """Core parameterized outcome scorer.

    Matches validate.py / verified_prior_replay.py conventions:
    - strict adds known-open print precedence and T1 -> BE single-bar check.
    - optimistic gives target precedence on ambiguous bars.
    - same_day forces EOD censoring at session end (15:29).
    """
    reached = False
    ambiguous = False
    last = start
    mae = 0.0
    mfe = 0.0

    for j in range(start, len(bars)):
        b = bars[j]
        if same_day and b['date'] != bars[start]['date']:
            break
        last = j
        op = side * (b['open'] - entry)
        lo = side * (b['low' if side == 1 else 'high'] - entry)
        hi = side * (b['high' if side == 1 else 'low'] - entry)

        # Track excursions (full-exit-bar upper bounds)
        adverse = max(0.0, -lo)
        if adverse > mae:
            mae = adverse
        favorable = max(0.0, hi)
        if favorable > mfe:
            mfe = favorable

        # Check known open precedence
        if strict:
            if op <= (0 if reached else -stop_distance):
                return (1 if reached else -1), ('T1_BE' if reached else 'SL'), j, ambiguous, mae, mfe
            if op >= target2_dist:
                return 2, 'T2', j, ambiguous, mae, mfe
            if op >= target1_dist:
                reached = True

        stop = 0 if reached else -stop_distance
        touches_stop = lo <= stop
        touches_target = hi >= (target2_dist if reached else target1_dist)
        if touches_stop and touches_target:
            ambiguous = True

        if optimistic:
            if hi >= target2_dist:
                return 2, 'T2', j, ambiguous, mae, mfe
            if not reached and hi >= target1_dist:
                reached = True
                if lo <= 0:
                    return 1, 'T1_BE', j, True, mae, mfe
                continue

        # Primary strict conservative
        if touches_stop:
            return (1 if reached else -1), ('T1_BE' if reached else 'SL'), j, ambiguous, mae, mfe

        if hi >= target2_dist:
            if strict and not reached and lo <= 0:
                return 1, 'T1_BE', j, True, mae, mfe
            return 2, 'T2', j, ambiguous, mae, mfe

        if hi >= target1_dist:
            reached = True
            if lo <= 0:
                return 1, 'T1_BE', j, True, mae, mfe

    return (1 if reached else None), ('T1_PENDING' if reached else 'PENDING'), last, ambiguous, mae, mfe


def simulate_variant(bars, cohort, variant_name, sl_dist_override=None):
    rows = []
    for c in cohort:
        side = c['side']
        entry_price = c['entry_price']
        start_idx = c['start_idx']
        sl_dist = c['orig_sl_dist'] if sl_dist_override is None else float(sl_dist_override)
        t1_dist = c['t1_dist']
        t2_dist = c['t2_dist']
        rr_ratio = round(t1_dist / sl_dist, 4)
        fails_min_rr_gate = bool(rr_ratio < 1.0)

        # Primary strict outcome
        score, outcome, last, amb, mae, mfe = score_trade(
            bars, start_idx, side, entry_price, sl_dist, t1_dist, t2_dist, strict=True
        )

        # Sensitivities
        opt_score, opt_outcome, _, _, _, _ = score_trade(
            bars, start_idx, side, entry_price, sl_dist, t1_dist, t2_dist, strict=True, optimistic=True
        )
        eod_score, eod_outcome, _, _, _, _ = score_trade(
            bars, start_idx, side, entry_price, sl_dist, t1_dist, t2_dist, strict=True, same_day=True
        )
        # Next available bar open entry sensitivity
        open_entry = c['open_entry_price']
        open_score, open_outcome, _, _, _, _ = score_trade(
            bars, start_idx, side, open_entry, sl_dist, t1_dist, t2_dist, strict=True
        )

        exit_bar_start = bars[last]['time'].isoformat()
        exit_known_at = (bars[last]['time'] + pd.Timedelta(minutes=1)).isoformat()

        rows.append({
            'signal_id': c['signal_id'],
            'signal_uuid': c['signal_uuid'],
            'trade_id': c['trade_id'],
            'variant': variant_name,
            'date': c['date'],
            'month': c['month'],
            'partition': c['partition'],
            'direction': c['direction'],
            'entry_price': entry_price,
            'sl_distance': sl_dist,
            'sl_price': round(entry_price - side * sl_dist, 2),
            't1_distance': t1_dist,
            't1_price': c['t1_price'],
            't2_distance': t2_dist,
            't2_price': c['t2_price'],
            'rr_ratio': rr_ratio,
            'fails_min_rr_gate': fails_min_rr_gate,
            'score': score,
            'outcome': outcome,
            'exit_index': last,
            'exit_time': exit_bar_start,
            'exit_bar_start': exit_bar_start,
            'exit_known_at': exit_known_at,
            'ambiguity': amb,
            'mae': round(mae, 2),
            'mfe': round(mfe, 2),
            'sl_risk_exposure': round(sl_dist if outcome == 'SL' else 0.0, 2),
            'optimistic_score': opt_score,
            'optimistic_outcome': opt_outcome,
            'eod_score': eod_score,
            'eod_outcome': eod_outcome,
            'open_entry_score': open_score,
            'open_entry_outcome': open_outcome,
        })
    return pd.DataFrame(rows)


def summarize_ledger(df, group_cols):
    rows = []
    for keys, g in df.groupby(group_cols, sort=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        score = g['score']
        resolved = score.dropna()
        n = len(g)
        n_t1_be = int((g['outcome'] == 'T1_BE').sum())
        n_t1_pending = int((g['outcome'] == 'T1_PENDING').sum())
        n_pending_before_t1 = int((g['outcome'] == 'PENDING').sum())
        n_total_unfinished = int(g['outcome'].isin(['T1_PENDING', 'PENDING']).sum())
        n_t2 = int((g['outcome'] == 'T2').sum())
        n_sl = int((g['outcome'] == 'SL').sum())
        tot_score = float(score.sum())
        per_entry = float(tot_score / n) if n > 0 else 0.0
        pos_pct = float((score > 0).sum() / len(resolved) * 100) if len(resolved) > 0 else 0.0
        tot_sl_exposure = float(g['sl_risk_exposure'].sum())
        fails_rr = int(g['fails_min_rr_gate'].sum())
        fails_rr_pct = float(fails_rr / n * 100) if n > 0 else 0.0

        rows.append(dict(zip(group_cols, keys)) | {
            'trades': n,
            't1_be': n_t1_be,
            't1_pending': n_t1_pending,
            'pending_before_t1': n_pending_before_t1,
            'total_unfinished': n_total_unfinished,
            't2': n_t2,
            'sl': n_sl,
            'pending': n_total_unfinished,
            'total_score': tot_score,
            'score_per_trade': round(per_entry, 4),
            'win_rate_pct': round(pos_pct, 2),
            'total_sl_risk_exposure': round(tot_sl_exposure, 2),
            'exposure_per_trade': round(tot_sl_exposure / n, 2) if n > 0 else 0.0,
            'failing_min_rr_count': fails_rr,
            'failing_min_rr_pct': round(fails_rr_pct, 2),
            'ambiguous_bars_count': int(g['ambiguity'].sum()),
        })
    return pd.DataFrame(rows)


def run_paired_bootstrap(control_df, candidate_df, all_session_dates, rng, n_resamples=2000):
    a = candidate_df.set_index('signal_id')
    b = control_df.set_index('signal_id').reindex(a.index)

    pairs_full = pd.DataFrame({
        'signal_id': a.index,
        'date': a['date'],
        'partition': a['partition'],
        'month': a['month'],
        'direction': a['direction'],
        'candidate_score': a['score'],
        'control_score': b['score'],
        'is_unresolved': a['score'].isna() | b['score'].isna(),
        'delta': a['score'] - b['score'],
    })

    dev_dates = [d for d in all_session_dates if d <= '2026-08-31']
    val_dates = [d for d in all_session_dates if d > '2026-08-31']

    blocks = [
        ('all', pairs_full, all_session_dates),
        ('development', pairs_full[pairs_full['partition'] == 'development'], dev_dates),
        ('historical_validation', pairs_full[pairs_full['partition'] == 'historical_validation'], val_dates),
        ('bullish', pairs_full[pairs_full['direction'] == 'BULLISH'], all_session_dates),
        ('bearish', pairs_full[pairs_full['direction'] == 'BEARISH'], all_session_dates),
    ]
    for m in sorted(pairs_full['month'].unique()):
        m_dates = [d for d in all_session_dates if d.startswith(m)]
        blocks.append((f'month_{m}', pairs_full[pairs_full['month'] == m], m_dates))

    results = []
    for name, block_full, block_dates in blocks:
        block_unresolved = int(block_full['is_unresolved'].sum())
        valid_block = block_full[~block_full['is_unresolved']]
        valid_count = len(valid_block)

        # Build daily delta array across ALL session dates in this block's calendar, including zero-delta no-trade days
        daily_sums = valid_block.groupby('date')['delta'].sum().to_dict()
        day_deltas = np.array([daily_sums.get(d, 0.0) for d in block_dates])
        n_days = len(day_deltas)
        observed_delta = float(day_deltas.sum())

        if n_days > 0:
            draws = rng.choice(day_deltas, (n_resamples, n_days), replace=True).sum(axis=1)
            ci_low = float(np.quantile(draws, 0.025))
            ci_high = float(np.quantile(draws, 0.975))
            frac_nonpos = float((draws <= 0).mean())
            boot_mean = float(draws.mean())
        else:
            ci_low, ci_high, frac_nonpos, boot_mean = 0.0, 0.0, 1.0, 0.0

        results.append({
            'block': name,
            'trades_count': valid_count,
            'sessions_count': n_days,
            'observed_delta': round(observed_delta, 2),
            'bootstrap_mean': round(boot_mean, 2),
            'ci_95_low': round(ci_low, 2),
            'ci_95_high': round(ci_high, 2),
            'fraction_bootstrap_delta_nonpositive': round(frac_nonpos, 4),
            'unresolved_pairs_excluded': block_unresolved,
        })
    return results


def update_progress(round_num, sl_dist, summary_dict):
    p_path = HERE / 'progress.json'
    prog = json.loads(p_path.read_text())
    key = f'round_{round_num}'
    prog['phases'][key]['status'] = 'COMPLETED'
    prog['phases'][key]['sl_distance'] = sl_dist
    prog['phases'][key]['summary'] = summary_dict
    prog['updated_utc'] = pd.Timestamp.now(tz='UTC').isoformat()
    if round_num == 3:
        prog['current_phase'] = 'ROUNDS_COMPLETED'
    p_path.write_text(json.dumps(prog, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--round', type=int, choices=[1, 2, 3], help='Execute a specific round (1, 2, or 3)')
    parser.add_argument('--final', action='store_true', help='Execute all variants, full paired analysis, bootstrap, and summaries')
    args = parser.parse_args()

    bars, cohort, sel = load_inputs()
    widths = sel['final_selected_widths']

    # Always generate baseline
    baseline_df = simulate_variant(bars, cohort, 'baseline', sl_dist_override=None)
    baseline_df.to_csv(HERE / 'baseline_ledger.csv', index=False)

    variants_map = {
        1: ('sl_22', widths[0]),
        2: ('sl_29', widths[1]),
        3: ('sl_30', widths[2]),
    }

    if args.round is not None:
        r = args.round
        vname, sl_w = variants_map[r]
        v_df = simulate_variant(bars, cohort, vname, sl_dist_override=sl_w)
        v_df.to_csv(HERE / f'{vname}_ledger.csv', index=False)
        v_df.to_csv(HERE / f'round{r}_ledger.csv', index=False)

        summ = summarize_ledger(v_df, ['variant']).iloc[0].to_dict()
        b_summ = summarize_ledger(baseline_df, ['variant']).iloc[0].to_dict()

        # Rescued and persisting counts
        rescued = int(((baseline_df['outcome'] == 'SL') & (v_df['outcome'].isin(['T1_BE', 'T2']))).sum())
        persisting_sl = int(((baseline_df['outcome'] == 'SL') & (v_df['outcome'] == 'SL')).sum())
        worsened = int((v_df['score'] < baseline_df['score']).sum())

        round_summary = {
            'sl_distance': sl_w,
            'total_score': summ['total_score'],
            'score_delta_vs_baseline': round(summ['total_score'] - b_summ['total_score'], 2),
            't1_be_count': summ['t1_be'],
            't2_count': summ['t2'],
            'sl_count': summ['sl'],
            'rescued_trades': rescued,
            'persisting_sl_trades': persisting_sl,
            'worsened_trades': worsened,
            'total_sl_risk_exposure': summ['total_sl_risk_exposure'],
            'risk_exposure_increase_pct': round((summ['total_sl_risk_exposure'] - b_summ['total_sl_risk_exposure']) / b_summ['total_sl_risk_exposure'] * 100, 2),
            'failing_min_rr_count': summ['failing_min_rr_count'],
            'failing_min_rr_pct': summ['failing_min_rr_pct'],
        }
        update_progress(r, sl_w, round_summary)
        print(f"Round {r} completed successfully: SL={sl_w}")
        print(json.dumps(round_summary, indent=2))
        return

    if args.final:
        print("Running full final multi-variant simulation...")
        all_dfs = [baseline_df]
        variant_dfs = {'baseline': baseline_df}

        for r in [1, 2, 3]:
            vname, sl_w = variants_map[r]
            v_df = simulate_variant(bars, cohort, vname, sl_dist_override=sl_w)
            v_df.to_csv(HERE / f'{vname}_ledger.csv', index=False)
            v_df.to_csv(HERE / f'round{r}_ledger.csv', index=False)
            all_dfs.append(v_df)
            variant_dfs[vname] = v_df

            summ = summarize_ledger(v_df, ['variant']).iloc[0].to_dict()
            b_summ = summarize_ledger(baseline_df, ['variant']).iloc[0].to_dict()
            rescued = int(((baseline_df['outcome'] == 'SL') & (v_df['outcome'].isin(['T1_BE', 'T2']))).sum())
            persisting_sl = int(((baseline_df['outcome'] == 'SL') & (v_df['outcome'] == 'SL')).sum())
            worsened = int((v_df['score'] < baseline_df['score']).sum())
            round_summary = {
                'sl_distance': sl_w,
                'total_score': summ['total_score'],
                'score_delta_vs_baseline': round(summ['total_score'] - b_summ['total_score'], 2),
                't1_be_count': summ['t1_be'],
                't2_count': summ['t2'],
                'sl_count': summ['sl'],
                'rescued_trades': rescued,
                'persisting_sl_trades': persisting_sl,
                'worsened_trades': worsened,
                'total_sl_risk_exposure': summ['total_sl_risk_exposure'],
                'risk_exposure_increase_pct': round((summ['total_sl_risk_exposure'] - b_summ['total_sl_risk_exposure']) / b_summ['total_sl_risk_exposure'] * 100, 2),
                'failing_min_rr_count': summ['failing_min_rr_count'],
                'failing_min_rr_pct': summ['failing_min_rr_pct'],
            }
            update_progress(r, sl_w, round_summary)

        full_ledger = pd.concat(all_dfs, ignore_index=True)
        full_ledger.to_csv(HERE / 'all_variants_ledger.csv', index=False)

        # Summaries
        summ_variants = summarize_ledger(full_ledger, ['variant'])
        summ_variants.to_csv(HERE / 'summary_variants.csv', index=False)

        summ_partitions = summarize_ledger(full_ledger, ['variant', 'partition'])
        summ_partitions.to_csv(HERE / 'summary_partitions.csv', index=False)

        summ_monthly = summarize_ledger(full_ledger, ['variant', 'month'])
        summ_monthly.to_csv(HERE / 'summary_monthly.csv', index=False)

        summ_dir = summarize_ledger(full_ledger, ['variant', 'direction'])
        summ_dir.to_csv(HERE / 'summary_directional.csv', index=False)

        summ_daily = summarize_ledger(full_ledger, ['variant', 'date'])
        summ_daily.to_csv(HERE / 'summary_daily.csv', index=False)

        # Sensitivities summary: switch outcome and recompute SL exposure
        sens_rows = []
        for vname, df_v in variant_dfs.items():
            for conv, score_col, out_col in [
                ('primary_strict', 'score', 'outcome'),
                ('optimistic_target_first', 'optimistic_score', 'optimistic_outcome'),
                ('eod_censored', 'eod_score', 'eod_outcome'),
                ('open_entry_price', 'open_entry_score', 'open_entry_outcome'),
            ]:
                temp = df_v.copy()
                temp['score'] = temp[score_col]
                temp['outcome'] = temp[out_col]
                temp['sl_risk_exposure'] = np.where(temp['outcome'] == 'SL', temp['sl_distance'], 0.0)
                s_table = summarize_ledger(temp, ['variant'])
                s_table['convention'] = conv
                sens_rows.append(s_table)
        pd.concat(sens_rows, ignore_index=True).to_csv(HERE / 'sensitivity_summary.csv', index=False)

        # Paired comparisons & Bootstrap across all 59 session dates
        all_session_dates = sorted(list(set(b['date'] for b in bars)))
        assert len(all_session_dates) == 59, f"Expected 59 session dates, got {len(all_session_dates)}"
        rng = np.random.default_rng(42)
        bootstrap_data = {}
        paired_summary_rows = []

        for r in [1, 2, 3]:
            vname, sl_w = variants_map[r]
            cand_df = variant_dfs[vname]
            boot_res = run_paired_bootstrap(baseline_df, cand_df, all_session_dates, rng, n_resamples=2000)
            bootstrap_data[vname] = boot_res

            for row in boot_res:
                paired_summary_rows.append({'candidate': vname, 'control': 'baseline'} | row)

        (HERE / 'bootstrap_results.json').write_text(json.dumps(bootstrap_data, indent=2) + '\n')
        pd.DataFrame(paired_summary_rows).to_csv(HERE / 'paired_comparisons.csv', index=False)

        # Risk compatibility JSON
        risk_doc = {
            'engine_min_rr_ratio': 1.0,
            'summary': summ_variants[['variant', 'trades', 'total_score', 'total_sl_risk_exposure',
                                      'failing_min_rr_count', 'failing_min_rr_pct']].to_dict('records'),
        }
        (HERE / 'risk_compatibility.json').write_text(json.dumps(risk_doc, indent=2) + '\n')

        # Update final progress
        p_path = HERE / 'progress.json'
        prog = json.loads(p_path.read_text())
        prog['status'] = 'ALL_ROUNDS_COMPLETED'
        prog['current_phase'] = 'READY_FOR_TESTS_AND_REPORT'
        prog['phases']['reporting_and_artifacts']['status'] = 'IN_PROGRESS'
        prog['updated_utc'] = pd.Timestamp.now(tz='UTC').isoformat()
        p_path.write_text(json.dumps(prog, indent=2) + '\n')

        print("\nAll simulations, ledgers, bootstrap, and risk tables generated successfully.")
        print(summ_variants.to_string(index=False))


if __name__ == '__main__':
    main()
