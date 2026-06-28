from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

import pandas as pd
from supabase import create_client, Client


def get_supabase_client(url: str, key: str) -> Client:
    return create_client(url, key)


def load_ares_trade_analytics(
    supabase: Client,
    months: int = 6,
) -> List[dict]:
    import dateutil.parser
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    cutoff_str = cutoff.isoformat()

    response = (
        supabase.table("trade_analytics")
        .select("*")
        .gte("entry_timestamp", cutoff_str)
        .execute()
    )
    return response.data if response.data else []


def load_historical_candles_from_dhan(
    dhan_client,
    security_id: str,
    exchange_segment: str,
    instrument_type: str,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    response = dhan_client.historical_daily_data(
        security_id=security_id,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
        from_date=from_date,
        to_date=to_date,
    )

    if not response or response.get("status") != "success":
        raise ValueError(f"Failed to fetch historical data: {response}")

    data = response.get("data", {})
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(data.get("timestamp", []), unit="s"),
        "open": data.get("open", []),
        "high": data.get("high", []),
        "low": data.get("low", []),
        "close": data.get("close", []),
        "volume": data.get("volume", []),
    })
    return df


def load_intraday_candles_from_dhan(
    dhan_client,
    security_id: str,
    exchange_segment: str,
    date: str,
    interval_minutes: int = 1,
) -> pd.DataFrame:
    if interval_minutes == 1:
        response = dhan_client.intraday_minute_data(
            security_id, exchange_segment, security_id, date, date
        )
    else:
        response = dhan_client.intraday_daily_data(
            security_id, exchange_segment, security_id, date, date
        )

    if not response or response.get("status") != "success":
        raise ValueError(f"Failed to fetch intraday data: {response}")

    data = response.get("data", {})
    time_key = "start_Time" if "start_Time" in data else "timestamp"
    ts_vals = data.get(time_key, [])

    parsed_ts = []
    for ts in ts_vals:
        if isinstance(ts, (int, float)):
            parsed_ts.append(datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts))
        else:
            from dateutil import parser
            parsed_ts.append(parser.parse(str(ts)))

    df = pd.DataFrame({
        "timestamp": parsed_ts,
        "open": data.get("open", []),
        "high": data.get("high", []),
        "low": data.get("low", []),
        "close": data.get("close", []),
        "volume": data.get("volume", []),
    })
    return df


def build_training_dataset(
    supabase: Optional[Client] = None,
    dhan_client=None,
    source: str = "ares",
    months: int = 6,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> pd.DataFrame:
    if source == "ares":
        if supabase is None:
            raise ValueError("supabase client required for ares source")
        records = load_ares_trade_analytics(supabase, months=months)
        from .labeling import label_from_ares_outcome
        return label_from_ares_outcome(records)

    elif source == "dhan":
        if dhan_client is None or not from_date or not to_date:
            raise ValueError("dhan_client, from_date, and to_date required for dhan source")
        df = load_historical_candles_from_dhan(
            dhan_client, "", "", "", from_date, to_date
        )
        from .labeling import label_candle_forward
        return label_candle_forward(df)

    else:
        raise ValueError(f"Unknown source: {source}")
