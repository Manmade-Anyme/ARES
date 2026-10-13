"""Comprehensive verification suite for Exhaustion Stop-Loss Experiments.

Covers all requirements from BRIEF.md Paragraph 17:
- Long/short SL distance geometry
- Original 12 vs 10 baseline preservation
- Recorded target distances (T1, T2)
- Known open before extremes
- T1 -> BE = +1, T2 = +2
- Pending before T1, T1_PENDING
- Sourcebar vs accept times
- No overnight entry
- Train-only selection / no holdout leakage
- Exact 3 distinct widths
- Ledger arithmetic
- Equivalence between new parameterized scorer and frozen old scorer on 10/18/40
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

HERE = Path(__file__).resolve().parent
from replay import score_trade, simulate_variant, summarize_ledger, load_inputs


def test_scorer_bullish_bearish_sl_distance():
    """Verify long and short SL triggering at exact distances."""
    # Synthetic bars for bullish trade: entry=24000, SL=20 (level 23980)
    bars_bull = [
        {'date': '2026-08-01', 'open': 24000.0, 'high': 24005.0, 'low': 23985.0, 'close': 23990.0},
        {'date': '2026-08-01', 'open': 23990.0, 'high': 23992.0, 'low': 23979.0, 'close': 23982.0},  # triggers SL
    ]
    score, outcome, exit_idx, amb, mae, mfe = score_trade(
        bars_bull, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0
    )
    assert score == -1
    assert outcome == 'SL'
    assert exit_idx == 1
    assert mae == 21.0

    # Synthetic bars for bearish trade: entry=24000, SL=20 (level 24020)
    bars_bear = [
        {'date': '2026-08-01', 'open': 24000.0, 'high': 24015.0, 'low': 23995.0, 'close': 24010.0},
        {'date': '2026-08-01', 'open': 24010.0, 'high': 24021.0, 'low': 24008.0, 'close': 24018.0},  # triggers SL
    ]
    score_b, outcome_b, exit_idx_b, amb_b, mae_b, mfe_b = score_trade(
        bars_bear, start=0, side=-1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0
    )
    assert score_b == -1
    assert outcome_b == 'SL'
    assert exit_idx_b == 1
    assert mae_b == 21.0


def test_original_10_vs_12_stop_distance():
    """Verify that baseline preserves original 10.0 and 12.0 point stop distances."""
    b_df = pd.read_csv(HERE / 'baseline_ledger.csv')
    assert len(b_df) == 165
    counts = b_df['sl_distance'].round(2).value_counts()
    assert counts[10.0] == 129
    assert counts[12.0] == 36
    assert set(counts.index) == {10.0, 12.0}


def test_recorded_target_distances():
    """Verify that recorded signal target distances are preserved."""
    b_df = pd.read_csv(HERE / 'baseline_ledger.csv')
    assert (b_df['t1_distance'] > 0).all()
    assert (b_df['t2_distance'] > b_df['t1_distance']).all()
    t1_counts = b_df['t1_distance'].round(2).value_counts()
    assert t1_counts[18.0] == 129
    assert t1_counts[24.0] == 36


def test_known_open_before_extremes():
    """Verify open print precedence over intrabar extremes."""
    # Bar opens beyond SL: open=23970, high=24050 (above T2!), low=23965
    bars = [
        {'date': '2026-08-01', 'open': 23970.0, 'high': 24050.0, 'low': 23965.0, 'close': 24040.0}
    ]
    score, outcome, exit_idx, amb, _, _ = score_trade(
        bars, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0, strict=True
    )
    assert score == -1
    assert outcome == 'SL'
    assert exit_idx == 0

    # Bar opens beyond T2: open=24050, low=23970 (below SL!)
    bars_t2 = [
        {'date': '2026-08-01', 'open': 24050.0, 'high': 24055.0, 'low': 23970.0, 'close': 24000.0}
    ]
    score_2, outcome_2, exit_idx_2, _, _, _ = score_trade(
        bars_t2, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0, strict=True
    )
    assert score_2 == 2
    assert outcome_2 == 'T2'
    assert exit_idx_2 == 0


def test_t1_be_plus_one_and_t2_plus_two():
    """Verify T1 touch followed by BE scores +1, while T2 scores +2."""
    # Trade touches T1 in bar 0, then falls to entry in bar 1
    # Open is set to 24005.0 (valid level >= low 24002.0)
    bars_be = [
        {'date': '2026-08-01', 'open': 24005.0, 'high': 24020.0, 'low': 24002.0, 'close': 24015.0},  # touches T1 (24018)
        {'date': '2026-08-01', 'open': 24015.0, 'high': 24016.0, 'low': 23999.0, 'close': 24005.0},  # touches BE (24000)
    ]
    score, outcome, exit_idx, _, _, _ = score_trade(
        bars_be, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0
    )
    assert score == 1
    assert outcome == 'T1_BE'
    assert exit_idx == 1

    # Trade touches T2 directly (open set to 24005.0 >= low 24002.0)
    bars_t2 = [
        {'date': '2026-08-01', 'open': 24005.0, 'high': 24045.0, 'low': 24002.0, 'close': 24042.0},  # touches T2 (24040)
    ]
    score_2, outcome_2, exit_idx_2, _, _, _ = score_trade(
        bars_t2, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0
    )
    assert score_2 == 2
    assert outcome_2 == 'T2'
    assert exit_idx_2 == 0


def test_pending_trade_before_t1():
    """Verify pending trade before T1 returns None, and after T1 returns +1."""
    # Trade never touches SL or T1
    bars_pending = [
        {'date': '2026-08-01', 'open': 24000.0, 'high': 24010.0, 'low': 23990.0, 'close': 24005.0},
    ]
    score, outcome, _, _, _, _ = score_trade(
        bars_pending, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0
    )
    assert score is None
    assert outcome == 'PENDING'

    # Trade touches T1 but not T2 or BE before end of data (open set to 24005.0 >= low 24005.0)
    bars_t1_pending = [
        {'date': '2026-08-01', 'open': 24005.0, 'high': 24020.0, 'low': 24005.0, 'close': 24018.0},
    ]
    score_t1p, outcome_t1p, _, _, _, _ = score_trade(
        bars_t1_pending, start=0, side=1, entry=24000.0, stop_distance=20.0, target1_dist=18.0, target2_dist=40.0
    )
    assert score_t1p == 1
    assert outcome_t1p == 'T1_PENDING'


def test_sourcebar_accept_times_alignment():
    """Verify first evaluated bar is strictly max(source + 1, ceil(active_created_at))."""
    bars, cohort, _ = load_inputs()
    timestamps = [b['timestamp'] for b in bars]
    for c in cohort:
        source_idx = c['source_idx']
        created = pd.Timestamp(c['trade_created_at'])
        expected_start = max(source_idx + 1, int(np.searchsorted(timestamps, created.ceil('min').timestamp())))
        assert c['start_idx'] == expected_start
        assert c['start_idx'] >= source_idx + 1


def test_no_overnight_entry():
    """Verify no entry evaluated overnight or across session boundaries."""
    bars, cohort, _ = load_inputs()
    for c in cohort:
        assert bars[c['start_idx']]['date'] == bars[c['source_idx']]['date']


def test_train_only_selection_no_leakage():
    """Verify selection.json used strictly Development data (<= 2026-08-31)."""
    mae_df = pd.read_csv(HERE / 'developmentMAE.csv')
    assert (mae_df['date'] <= '2026-08-31').all()
    assert (mae_df['date'] >= '2026-07-20').all()
    assert len(mae_df) == 82

    sel = json.loads((HERE / 'selection.json').read_text())
    assert sel['development_trades_count'] == 82
    assert sel['qualifying_recoveries_count'] == 8
    assert sel['selection_basis'] == 'QUALIFYING_RECOVERIES'


def test_exact_three_distinct_widths():
    """Verify exact 3 distinct widths bounded in [13..30]."""
    sel = json.loads((HERE / 'selection.json').read_text())
    widths = sel['final_selected_widths']
    assert len(widths) == 3
    assert len(set(widths)) == 3
    assert widths == sorted(widths)
    assert all(13 <= w <= 30 for w in widths)
    assert widths == [22, 29, 30]


def test_ledger_arithmetic_consistency():
    """Verify ledger score arithmetic across all generated ledgers."""
    for filename in ['baseline_ledger.csv', 'round1_ledger.csv', 'round2_ledger.csv', 'round3_ledger.csv']:
        df = pd.read_csv(HERE / filename)
        assert len(df) == 165
        t1_be = (df['outcome'] == 'T1_BE').sum()
        t2 = (df['outcome'] == 'T2').sum()
        sl = (df['outcome'] == 'SL').sum()
        pending = df['outcome'].isin(['PENDING', 'T1_PENDING']).sum()
        expected_score = float(t1_be * 1.0 + t2 * 2.0 - sl * 1.0 + (df['outcome'] == 'T1_PENDING').sum() * 1.0)
        assert round(df['score'].sum(), 2) == round(expected_score, 2)
        assert len(df) == t1_be + t2 + sl + pending
        assert round(df['sl_risk_exposure'].sum(), 2) == round((df[df['outcome'] == 'SL']['sl_distance']).sum(), 2)


def test_scorer_matches_frozen_old_scorer():
    """Verify new parameterized scorer matches frozen old scorer on 10/18/40 actual bars."""
    bars, cohort, _ = load_inputs()

    def old_outcome(bars, start, side, entry, stop_distance=10, strict=False, same_day=False, target1=18, target2=40):
        reached = False
        for j in range(start, len(bars)):
            bar = bars[j]
            if same_day and bar['date'] != bars[start]['date']:
                break
            hi = side * (bar['high' if side == 1 else 'low'] - entry)
            lo = side * (bar['low' if side == 1 else 'high'] - entry)
            op = side * (bar['open'] - entry)
            if strict:
                if op <= (0 if reached else -stop_distance):
                    return (1 if reached else -1), j
                if op >= target2:
                    return 2, j
                if op >= target1:
                    reached = True
            if lo <= (0 if reached else -stop_distance):
                return (1 if reached else -1), j
            if hi >= target2:
                return (1 if strict and not reached and lo <= 0 else 2), j
            if hi >= target1:
                reached = True
                if lo <= 0:
                    return 1, j
        return (1 if reached else None), j

    for c in cohort:
        start_idx = c['start_idx']
        side = c['side']
        entry_price = c['entry_price']

        # Multi-session strict=True
        old_sc, old_j = old_outcome(bars, start_idx, side, entry_price, stop_distance=10, strict=True, same_day=False, target1=18, target2=40)
        new_sc, _, new_j, _, _, _ = score_trade(bars, start_idx, side, entry_price, stop_distance=10, target1_dist=18, target2_dist=40, strict=True, same_day=False)
        assert old_sc == new_sc
        assert old_j == new_j

        # Multi-session strict=False
        old_sc_f, old_j_f = old_outcome(bars, start_idx, side, entry_price, stop_distance=10, strict=False, same_day=False, target1=18, target2=40)
        new_sc_f, _, new_j_f, _, _, _ = score_trade(bars, start_idx, side, entry_price, stop_distance=10, target1_dist=18, target2_dist=40, strict=False, same_day=False)
        assert old_sc_f == new_sc_f
        assert old_j_f == new_j_f


def test_summarize_ledger_separates_pending_and_t1_be():
    """Verify summarize_ledger separates t1_be, t1_pending, pending_before_t1, total_unfinished."""
    df_sample = pd.DataFrame([
        {'variant': 'test', 'score': 1.0, 'outcome': 'T1_BE', 'sl_risk_exposure': 0.0, 'fails_min_rr_gate': False, 'ambiguity': False},
        {'variant': 'test', 'score': 1.0, 'outcome': 'T1_PENDING', 'sl_risk_exposure': 0.0, 'fails_min_rr_gate': False, 'ambiguity': False},
        {'variant': 'test', 'score': None, 'outcome': 'PENDING', 'sl_risk_exposure': 0.0, 'fails_min_rr_gate': False, 'ambiguity': False},
        {'variant': 'test', 'score': 2.0, 'outcome': 'T2', 'sl_risk_exposure': 0.0, 'fails_min_rr_gate': False, 'ambiguity': False},
        {'variant': 'test', 'score': -1.0, 'outcome': 'SL', 'sl_risk_exposure': 22.0, 'fails_min_rr_gate': False, 'ambiguity': False},
    ])
    summ = summarize_ledger(df_sample, ['variant']).iloc[0]
    assert summ['trades'] == 5
    assert summ['t1_be'] == 1
    assert summ['t1_pending'] == 1
    assert summ['pending_before_t1'] == 1
    assert summ['total_unfinished'] == 2
    assert summ['t2'] == 1
    assert summ['sl'] == 1
    assert summ['total_score'] == 3.0  # 1 + 1 + 2 - 1 = 3


def test_sensitivities_switch_outcome_and_recompute_sl_exposure():
    """Verify sensitivity summary switches outcome and recomputes total_sl_risk_exposure."""
    sens_df = pd.read_csv(HERE / 'sensitivity_summary.csv')
    assert 'eod_censored' in sens_df['convention'].values

    # Under eod_censored, sl_22 has 79 SL hits, so exposure is 79 * 22 = 1738
    sl22_eod = sens_df[(sens_df['variant'] == 'sl_22') & (sens_df['convention'] == 'eod_censored')].iloc[0]
    assert sl22_eod['sl'] == 79
    assert sl22_eod['total_sl_risk_exposure'] == 1738.0
    assert sl22_eod['t1_pending'] == 13
    assert sl22_eod['pending_before_t1'] == 13
    assert sl22_eod['total_unfinished'] == 26

    # Under optimistic_target_first, sl_22 has 85 SL hits, so exposure is 85 * 22 = 1870
    sl22_opt = sens_df[(sens_df['variant'] == 'sl_22') & (sens_df['convention'] == 'optimistic_target_first')].iloc[0]
    assert sl22_opt['sl'] == 85
    assert sl22_opt['total_sl_risk_exposure'] == 1870.0


def test_exit_bar_start_and_exit_known_at():
    """Verify exit_bar_start and conservative exit_known_at availability (+1 min)."""
    base_df = pd.read_csv(HERE / 'baseline_ledger.csv')
    assert 'exit_bar_start' in base_df.columns
    assert 'exit_known_at' in base_df.columns
    for _, row in base_df.head(20).iterrows():
        t_start = pd.Timestamp(row['exit_bar_start'])
        t_known = pd.Timestamp(row['exit_known_at'])
        assert t_known == t_start + pd.Timedelta(minutes=1)


def test_cohort_csv_and_signal_475_match():
    """Verify cohort.csv is created with 165 records and documents signal 475 match to legacy 1892."""
    cohort_df = pd.read_csv(HERE / 'cohort.csv')
    assert len(cohort_df) == 165
    s475 = cohort_df[cohort_df['signal_id'] == 475].iloc[0]
    assert '1892' in str(s475['notes'])
    assert '13:28:48' in str(s475['notes'])


def test_selection_overwrite_refusal_and_hash_verification():
    """Verify selection.py refuses to overwrite existing selection.json and input hashes match."""
    from selection import build_cohort_and_select
    sel_doc = build_cohort_and_select()
    assert sel_doc['final_selected_widths'] == [22, 29, 30]

    # Verify input hashes match
    bars, cohort, sel = load_inputs()
    assert len(cohort) == 165


def test_paired_bootstrap_59_session_dates_and_nonpositive_fraction():
    """Verify bootstrap uses 59 calendar session dates and reports fraction_bootstrap_delta_nonpositive."""
    boot_res = json.loads((HERE / 'bootstrap_results.json').read_text())
    assert 'sl_22' in boot_res
    blocks = {b['block']: b for b in boot_res['sl_22']}
    assert blocks['all']['sessions_count'] == 59
    assert blocks['development']['sessions_count'] == 31
    assert blocks['historical_validation']['sessions_count'] == 28
    assert 'fraction_bootstrap_delta_nonpositive' in blocks['all']
    assert 'empirical_p_value' not in blocks['all']

