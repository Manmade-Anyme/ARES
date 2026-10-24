"""Offline migration preview cannot mutate history or silently approve a carry."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'preview_option_atr_migration.py'
SPEC = importlib.util.spec_from_file_location('option_atr_migration_preview', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_missing_evidence_is_explicit_and_never_inferred_from_price():
    export = {'active_trades': [{'id': 'x', 'state': 'T1_HIT', 'current_price': 999}],
              'trade_analytics': [{'id': 'x', 'result_state': 'OPEN', 'market_context': {}}]}
    original = deepcopy(export)
    row = MODULE.preview_option_atr_migration(export)['trades'][0]
    assert row['eligible'] is False
    assert row['requires_approval'] is True
    assert row['activation_allowed'] is False
    for requirement in ('known_timezone_aware_registration_timestamp_required',
                        'complete_original_pinned_contract_and_option_bracket_required',
                        'verified_chart_parity_signoff_required',
                        'backend_transaction_schema_verification_required',
                        'explicit_complete_milestone_history_with_source_required'):
        assert requirement in row['requirements']
    assert row['write_operations'] == []
    assert export == original


@pytest.mark.parametrize('active_state,result_state,context,expected', [
    ('CLOSED', 'ATR_SL_HIT', {}, 'CLOSED_HISTORY_UNCHANGED'),
    ('OPEN', 'SL_HIT', {}, 'CLOSED_HISTORY_UNCHANGED'),
    ('T2_REACHED', 'OPEN', {'option_trailing': {'policy_version': 'option_atr_v1'}}, 'ALREADY_VERSIONED_UNCHANGED'),
])
def test_closed_history_and_enrolled_trades_have_no_correction(active_state, result_state, context, expected):
    export = {'active_trades': [{'id': 'x', 'state': active_state}],
              'trade_analytics': [{'id': 'x', 'result_state': result_state, 'market_context': context}]}
    row = MODULE.preview_option_atr_migration(export)['trades'][0]
    assert row['status'] == expected
    assert row['eligible'] is False
    assert row['write_operations'] == []
    assert row['replay_historical_alerts'] is False


def test_cli_is_offline_stdout_preview_without_apply_flag(tmp_path):
    source = tmp_path / 'export.json'
    source.write_text('{"active_trades": [], "trade_analytics": []}')
    output = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True, check=True)
    assert 'OFFLINE_PREVIEW_ONLY' in output.stdout
    assert '"writes_performed": 0' in output.stdout
    blocked = subprocess.run([sys.executable, str(SCRIPT), str(source), '--apply'], capture_output=True, text=True)
    assert blocked.returncode != 0
    assert 'unrecognized arguments: --apply' in blocked.stderr


def complete_export():
    from datetime import datetime, timedelta
    prior = datetime.fromisoformat('2026-10-22T14:20:00+05:30')
    starts = [prior + timedelta(minutes=5*i) for i in range(14)]
    starts.append(datetime.fromisoformat('2026-10-23T09:15:00+05:30'))
    candles = [{'start_time': start.isoformat(), 'end_time': (start+timedelta(minutes=5)).isoformat(),
                'open': 100, 'high': 110, 'low': 90, 'close': 105, 'security_id': '51333', 'complete': True}
               for start in starts]
    evidence = {'registered_at': '2026-10-23T09:16:00+05:30', 'registration_source': 'registration-audit-log',
                'option_candles': candles, 'history_verified_through': '2026-10-23T09:20:00+05:30',
                'milestone_history_source': 'index-candles-export',
                'milestone_events': [{'type': 'T1_HIT', 'event_time': '2026-10-23T09:17:00+05:30', 'source': 'index-0916'}],
                'parity_signoff': {'policy_version': 'option_atr_v1', 'evidence_ref': 'declared-parity-report'},
                'schema_verification': {'migration': MODULE.MIGRATION, 'evidence_ref': 'declared-schema-verification'},
                'chronological_replay': {'policy_version': 'option_atr_v1', 'terminal': False,
                                         'highest_milestone': 1, 'evidence_ref': 'declared-replay'}}
    return {'active_trades': [{'id': 'x', 'state': 'T1_HIT', 'entry_timestamp': '2026-10-23T09:16:00+05:30',
                               'entry_price': 22552.1, 'target_1': 22527.1, 'target_2': 22472.1}],
            'trade_analytics': [{'id': 'x', 'result_state': 'OPEN', 'market_context': {
                'options_sizing': {'security_id': '51333', 'expiry_date': '2026-10-27', 'strike': 22550,
                                  'option_type': 'PE', 'entry_limit': 100., 'option_sl': 90., 'synthetic_spot_sl': 22577.1},
                'option_atr_migration_evidence': evidence}}]}


def test_full_declared_evidence_only_produces_human_review_candidate():
    report = MODULE.preview_option_atr_migration(complete_export())
    row = report['trades'][0]
    assert row['eligible'] is True
    assert row['status'] == 'HUMAN_REVIEW_CANDIDATE'
    assert row['requires_approval'] is True
    assert row['activation_allowed'] is False
    assert row['proposed_remaining_fraction'] == .5
    assert report['writes_performed'] == 0
    assert report['historical_alert_replay'] is False


@pytest.mark.parametrize('defect,expected', [
    ('gap', 'option_history_gap_requires_backfill'),
    ('wrong_contract', 'invalid_or_incomplete_pinned_option_history'),
    ('forming', 'invalid_or_incomplete_pinned_option_history'),
    ('milestone', 'chronological_sourced_t1_t2_history_required'),
    ('terminal_replay', 'reviewed_nonterminal_chronological_replay_required'),
])
def test_incomplete_or_contradictory_evidence_cannot_qualify(defect, expected):
    export = complete_export()
    evidence = export['trade_analytics'][0]['market_context']['option_atr_migration_evidence']
    if defect == 'gap':
        evidence['option_candles'].pop(4)
    elif defect == 'wrong_contract':
        evidence['option_candles'][0]['security_id'] = '51331'
    elif defect == 'forming':
        evidence['option_candles'][-1]['complete'] = False
    elif defect == 'milestone':
        evidence['milestone_events'][0]['type'] = 'T2_HIT'
    else:
        evidence['chronological_replay']['terminal'] = True
    row = MODULE.preview_option_atr_migration(export)['trades'][0]
    assert row['eligible'] is False
    assert expected in row['requirements']


def test_signal_entry_timestamp_alone_is_not_registration_proof():
    export = complete_export()
    evidence = export['trade_analytics'][0]['market_context']['option_atr_migration_evidence']
    evidence.pop('registered_at')
    row = MODULE.preview_option_atr_migration(export)['trades'][0]
    assert row['eligible'] is False
    assert 'known_timezone_aware_registration_timestamp_required' in row['requirements']
