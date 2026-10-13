"""Read-only Dhan capture and data quality verification for July 20 - Oct 13, 2026."""
import contextlib
import hashlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))


def run_preflight_and_capture():
    from config import settings
    from storage import load_dhan_credentials_from_supabase
    from dhanhq import DhanContext, DhanLogin, dhanhq

    # 1. Silently load credentials from Supabase
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        load_dhan_credentials_from_supabase()

    # 2. Check profile validity silently
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        response = DhanLogin(settings.dhan_client_id).user_profile(settings.dhan_access_token)

    profile = response.get('data', response) if isinstance(response, dict) else {}
    safe_profile = {
        'dataPlan': profile.get('dataPlan'),
        'dataValidity': profile.get('dataValidity'),
        'tokenValidity': profile.get('tokenValidity'),
        'checked_utc': datetime.now(timezone.utc).isoformat(),
        'supabase_credential_read': 'success',
    }
    (HERE / 'preflight.json').write_text(json.dumps(safe_profile, indent=2) + '\n')

    data_plan_status = str(profile.get('dataPlan', '')).lower()
    if data_plan_status not in {'active', 'true', '1'}:
        raise RuntimeError(f"Dhan dataPlan preflight not active: {data_plan_status}")

    # 3. Fetch minute data for security_id 13 (NIFTY Index) from 2026-07-20 to 2026-10-13
    client = dhanhq(DhanContext(settings.dhan_client_id, settings.dhan_access_token))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        hist_response = client.intraday_minute_data(
            security_id='13',
            exchange_segment='IDX_I',
            instrument_type='INDEX',
            from_date='2026-07-20 00:00:00',
            to_date='2026-10-13 00:00:00',
            interval=1,
        )

    if not isinstance(hist_response, dict) or hist_response.get('status') != 'success':
        err_msg = hist_response.get('remarks') if isinstance(hist_response, dict) else str(hist_response)
        raise RuntimeError(f"Historical minute data request failed: {err_msg}")

    data_payload = hist_response['data']
    raw_json_str = json.dumps(data_payload)
    (HERE / 'dhan_raw.json').write_text(raw_json_str)

    sha256_hash = hashlib.sha256(raw_json_str.encode('utf-8')).hexdigest()
    n_bars = len(data_payload['timestamp']) if isinstance(data_payload, dict) and 'timestamp' in data_payload else len(data_payload)

    capture_meta = {
        'captured_utc': datetime.now(timezone.utc).isoformat(),
        'security_id': '13',
        'exchange_segment': 'IDX_I',
        'instrument_type': 'INDEX',
        'interval_minutes': 1,
        'requested_from': '2026-07-20 00:00:00',
        'requested_to': '2026-10-13 00:00:00',
        'rows_received': n_bars,
        'sha256': sha256_hash,
    }
    (HERE / 'capture.json').write_text(json.dumps(capture_meta, indent=2) + '\n')

    # Also do separate Oct 12 check as requested by BRIEF
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        oct12_response = client.intraday_minute_data(
            security_id='13',
            exchange_segment='IDX_I',
            instrument_type='INDEX',
            from_date='2026-10-12 00:00:00',
            to_date='2026-10-13 00:00:00',
            interval=1,
        )
    oct12_rows = 0
    if isinstance(oct12_response, dict) and oct12_response.get('status') == 'success':
        oct12_data = oct12_response.get('data', {})
        if isinstance(oct12_data, dict) and 'timestamp' in oct12_data:
            oct12_rows = len(oct12_data['timestamp'])
        elif isinstance(oct12_data, list):
            oct12_rows = len(oct12_data)

    oct12_meta = {
        'status': oct12_response.get('status') if isinstance(oct12_response, dict) else 'unknown',
        'rows': oct12_rows,
        'requested_from': '2026-10-12 00:00:00',
        'requested_to': '2026-10-13 00:00:00',
        'remarks': oct12_response.get('remarks') if isinstance(oct12_response, dict) else str(oct12_response),
    }
    (HERE / 'oct12_availability.json').write_text(json.dumps(oct12_meta, indent=2) + '\n')

    return capture_meta


def validate_data():
    raw_path = HERE / 'dhan_raw.json'
    if not raw_path.exists():
        raise FileNotFoundError(f"Missing {raw_path}")

    raw_data = json.loads(raw_path.read_text())
    df = pd.DataFrame(raw_data)
    initial_rows = len(df)

    # Validate timestamps and convert to IST
    df['time'] = pd.to_datetime(df['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    df['date'] = df['time'].dt.strftime('%Y-%m-%d')
    df['hhmm'] = df['time'].dt.strftime('%H:%M')

    # Retain strictly trading hours 09:15 to 15:29
    trading_df = df[(df['hhmm'] >= '09:15') & (df['hhmm'] < '15:30')].copy()
    trading_df = trading_df.sort_values('timestamp').reset_index(drop=True)

    # Check duplicates and monotonicity
    duplicates = int(trading_df.duplicated(subset=['timestamp']).sum())
    is_monotonic = bool(trading_df['timestamp'].is_monotonic_increasing)

    # Check OHLC bounds
    valid_ohlc = bool(
        (trading_df[['open', 'high', 'low', 'close', 'volume']].notna().all().all())
        and (trading_df['volume'] >= 0).all()
        and (trading_df['high'] >= trading_df[['open', 'close', 'low']].max(axis=1)).all()
        and (trading_df['low'] <= trading_df[['open', 'close', 'high']].min(axis=1)).all()
    )

    # Session-by-session completeness check
    sessions = sorted(trading_df['date'].unique())
    complete_sessions = []
    malformed_sessions = []

    for d, g in trading_df.groupby('date'):
        bar_count = len(g)
        diffs = np.diff(g['timestamp'].values)
        has_gaps = not (diffs == 60).all() if len(diffs) > 0 else True
        has_open_bar = (g['hhmm'].iloc[0] == '09:15')
        has_close_bar = (g['hhmm'].iloc[-1] == '15:29')

        if bar_count == 375 and not has_gaps and has_open_bar and has_close_bar:
            complete_sessions.append(d)
        else:
            malformed_sessions.append({
                'date': d,
                'bar_count': bar_count,
                'has_gaps': has_gaps,
                'first_bar': g['hhmm'].iloc[0] if len(g) > 0 else None,
                'last_bar': g['hhmm'].iloc[-1] if len(g) > 0 else None,
            })

    # Partition assignments
    # Development: 2026-07-20 to 2026-08-31
    # Historical Validation: 2026-09-01 to end available
    dev_sessions = [d for d in complete_sessions if d <= '2026-08-31']
    val_sessions = [d for d in complete_sessions if d >= '2026-09-01']

    quality_meta = {
        'raw_rows': initial_rows,
        'trading_hours_rows': len(trading_df),
        'duplicates': duplicates,
        'is_monotonic': is_monotonic,
        'valid_ohlc': valid_ohlc,
        'total_sessions': len(sessions),
        'complete_sessions_count': len(complete_sessions),
        'malformed_sessions_count': len(malformed_sessions),
        'malformed_sessions': malformed_sessions,
        'development_sessions_count': len(dev_sessions),
        'development_sessions': dev_sessions,
        'historical_validation_sessions_count': len(val_sessions),
        'historical_validation_sessions': val_sessions,
    }
    (HERE / 'quality.json').write_text(json.dumps(quality_meta, indent=2) + '\n')
    return quality_meta


def main():
    print("Executing read-only Dhan preflight and capture...")
    capture_meta = run_preflight_and_capture()
    print(f"Captured {capture_meta['rows_received']} raw bars. SHA256: {capture_meta['sha256']}")

    print("Validating data quality and completeness...")
    quality_meta = validate_data()
    print(f"Validated {quality_meta['complete_sessions_count']} complete sessions (Dev: {quality_meta['development_sessions_count']}, Validation: {quality_meta['historical_validation_sessions_count']}).")
    if quality_meta['malformed_sessions_count'] > 0:
        print(f"WARNING: Found {quality_meta['malformed_sessions_count']} malformed sessions: {quality_meta['malformed_sessions']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
