"""Pure, contemporaneous OI-watch evidence; no ML placeholders or private data."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from math import isfinite, sqrt
from statistics import pstdev
from typing import Any
from zoneinfo import ZoneInfo

CONTEXT_VERSION = "oi-watch-v1"
IST = ZoneInfo("Asia/Kolkata")


def _get(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _timestamp(value):
    if isinstance(value, str):
        # PostgreSQL emits variable fractional precision; Python 3.10 accepts
        # exactly three or six fractional digits in fromisoformat.
        import re
        value = re.sub(r"\.(\d+)(?=[+-]|$)", lambda m: "." + m[1][:6].ljust(6, "0"), value.replace("Z", "+00:00"))
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("watch observation requires a datetime")
    # Existing candle contract uses naive exchange-local timestamps.
    return (value.replace(tzinfo=IST) if value.tzinfo is None else value).astimezone(IST)


def build_watch_candles(data, sampled_at):
    """Convert the same fetched Dhan arrays into candles with sample provenance.

    Completion is evaluated when the response was sampled, never by aging a
    previously partial candle in a rolling buffer. No request is made here.
    Malformed arrays or sample times yield unavailable evidence (an empty list).
    """
    try:
        sampled = _timestamp(sampled_at)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    time_key = "start_Time" if "start_Time" in data else "timestamp"
    keys = (time_key, "open", "high", "low", "close", "volume")
    if any(not isinstance(data.get(key), (list, tuple)) for key in keys):
        return []
    count = len(data[time_key])
    if any(len(data[key]) != count for key in keys):
        return []
    rows = {}
    for index in range(count):
        value = data[time_key][index]
        try:
            epoch = _number(value)
            if epoch is not None:
                at = datetime.fromtimestamp(epoch/1000 if epoch > 1e11 else epoch, timezone.utc).astimezone(IST)
            else:
                at = _timestamp(value)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if (at > sampled or at.date() != sampled.date()
                or not time(9, 15) <= at.time() < time(15, 30)):
            continue
        values = {key: _number(data[key][index]) for key in keys[1:]}
        if any(values[key] is None or values[key] <= 0 for key in ("open", "high", "low", "close")):
            continue
        if values["volume"] is None or values["volume"] < 0:
            continue
        if (values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"])):
            continue
        rows[at] = {"timestamp": at.isoformat(), "sampled_at": sampled.isoformat(),
                    "complete": at + timedelta(minutes=1) <= sampled, **values}
    return [rows[at] for at in sorted(rows)]


def build_watch_context(bias, spot, observed_at, *, candles, full_chain, levels,
                        pdh, pdl, expiry_date, minimum_move_points=25.0):
    """Freeze JSON-safe public evidence known at ``observed_at``.

    Candle timestamps denote one-minute bar starts. Completion requires explicit
    provider sample provenance after bar end and no later than observation.
    Stored partial bars never become completed merely because time passes.
    Session extrema are unavailable when the bounded buffer omits session open.
    """
    observed = _timestamp(observed_at)
    spot, minimum = _number(spot), _number(minimum_move_points)
    direction = _get(bias, "direction")
    direction = getattr(direction, "value", direction)
    if direction not in ("BEARISH", "BULLISH") or spot is None or spot <= 0 or minimum is None or minimum <= 0:
        raise ValueError("invalid watch direction, spot or minimum move")
    session_open = datetime.combine(observed.date(), time(9, 15), tzinfo=IST)
    session_close = datetime.combine(observed.date(), time(15, 30), tzinfo=IST)
    rows = {}
    for candle in candles or ():
        try:
            at = _timestamp(_get(candle, "timestamp"))
        except (ValueError, TypeError):
            continue
        if not session_open <= at <= observed or at >= session_close:
            continue
        sampled = _get(candle, "sampled_at")
        if sampled is not None:
            try:
                sampled = _timestamp(sampled)
            except (ValueError, TypeError):
                continue
            if sampled > observed or sampled < at:
                continue
        values = {k: _number(_get(candle, k)) for k in ("open", "high", "low", "close", "volume")}
        if any(values[k] is None or values[k] <= 0 for k in ("open", "high", "low", "close")):
            continue
        if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"]):
            continue
        rows[at] = {"timestamp": at.isoformat(), "sampled_at": sampled.isoformat() if sampled else None,
                   **values, "complete": _get(candle, "complete") is True and sampled is not None
                   and at + timedelta(minutes=1) <= sampled <= observed}
    ordered = sorted(rows)
    complete = [at for at in ordered if rows[at]["complete"]]
    returns = {}
    last = complete[-1] if complete else None
    for minutes in (5, 15, 30, 60):
        window = [last - timedelta(minutes=i) for i in range(minutes + 1)] if last else []
        returns[f"{minutes}m"] = (rows[last]["close"] - rows[window[-1]]["close"]
                                 if window and all(at in rows and rows[at]["complete"] for at in window) else None)
    contiguous = []
    for at in reversed(complete):
        if contiguous and contiguous[-1] - at != timedelta(minutes=1):
            break
        contiguous.append(at)
    contiguous.reverse()
    changes = [rows[b]["close"] - rows[a]["close"] for a, b in zip(contiguous, contiguous[1:])]
    ranges = []
    for i, at in enumerate(contiguous):
        row = rows[at]
        prev = rows[contiguous[i - 1]]["close"] if i else row["open"]
        ranges.append(max(row["high"]-row["low"], abs(row["high"]-prev), abs(row["low"]-prev)))
    net = sum(changes) if changes else None
    path = sum(abs(x) for x in changes)
    swings = {"highs": [], "lows": []}
    for a, b, c in zip(contiguous, contiguous[1:], contiguous[2:]):
        if rows[b]["high"] > max(rows[a]["high"], rows[c]["high"]):
            swings["highs"].append({"timestamp": b.isoformat(), "price": rows[b]["high"]})
        if rows[b]["low"] < min(rows[a]["low"], rows[c]["low"]):
            swings["lows"].append({"timestamp": b.isoformat(), "price": rows[b]["low"]})
    swings = {k: v[-3:] for k, v in swings.items()}
    expected = max(0, int((min(observed, session_close).replace(second=0, microsecond=0)-session_open).total_seconds()//60))
    covers_session = bool(expected and all(session_open + timedelta(minutes=i) in complete for i in range(expected)))
    opening_times = [session_open + timedelta(minutes=i) for i in range(15)]
    opening_complete = observed >= session_open + timedelta(minutes=15) and all(at in complete for at in opening_times)
    extrema = list(rows.values())
    wall = {key: _number(_get(bias, key)) for key in (
        "wall_strike", "wall_oi", "wall_oi_change_pct", "relative_percentile",
        "persistence_snapshots", "persistence_duration_seconds", "favourable_excursion_pts")}
    wall.update(wall_key=str(_get(bias, "wall_key", "")), wall_option_type=_get(bias, "wall_option_type"))
    for key in ("first_seen", "last_seen"):
        value = _get(bias, key)
        wall[key] = _timestamp(value).isoformat() if value else None
    interaction_at = _get(bias, "initial_interaction_timestamp")
    interaction_price = _number(_get(bias, "initial_interaction_price"))
    interaction_invalid = False
    if interaction_at is not None:
        try:
            interaction_at = _timestamp(interaction_at)
            interaction_invalid = not session_open <= interaction_at <= observed
        except (ValueError, TypeError):
            interaction_invalid = True
    wall["initial_interaction_timestamp"] = interaction_at.isoformat() if interaction_at is not None and not interaction_invalid else None
    wall["initial_interaction_price"] = interaction_price if interaction_at is not None and not interaction_invalid else None
    chain = []
    invalid_strike_count = 0
    for row in full_chain or ():
        strike = _number(_get(row, "strike"))
        if strike is None or strike <= 0:
            invalid_strike_count += 1
            continue
        chain.append({"strike": strike, **{f"{side}_{k}": _number(_get(row, f"{side}_{k}"))
            for side in ("ce", "pe") for k in ("oi", "oi_prev", "oi_change_pct", "iv", "delta")}})
        for side in ("ce", "pe"):
            for key in ("oi", "oi_prev"):
                field = f"{side}_{key}"
                if chain[-1][field] is not None and chain[-1][field] < 0:
                    chain[-1][field] = None
    opposing_side = "pe" if direction == "BEARISH" else "ce"
    ahead = lambda value: value < spot if direction == "BEARISH" else value > spot
    opposing = sorted([{"strike": r["strike"], "option_type": opposing_side.upper(), "oi": r[f"{opposing_side}_oi"]}
        for r in chain if ahead(r["strike"]) and (r[f"{opposing_side}_oi"] or 0) > 0], key=lambda r: r["oi"], reverse=True)[:3]
    level_rows = [{"price": _number(_get(level, "price")), "source": str(_get(level, "source", "")),
                   "strength": _number(_get(level, "strength"))} for level in levels or ()]
    level_rows = [r for r in level_rows if r["price"] is not None and r["price"] > 0]
    directional_levels = sorted([r for r in level_rows if ahead(r["price"])], key=lambda r: abs(r["price"]-spot))
    nearest = dict(directional_levels[0], distance_points=abs(directional_levels[0]["price"]-spot)) if directional_levels else None
    missing_oi = {side: sum(r[f"{side}_oi"] is None for r in chain) + invalid_strike_count for side in ("ce", "pe")}
    totals = {side: sum(r[f"{side}_oi"] for r in chain) if chain and missing_oi[side] == 0 else None for side in ("ce", "pe")}
    atm = min(chain, key=lambda r: abs(r["strike"]-spot)) if chain else None
    newest = ordered[-1] if ordered else None
    return {
        "context_version": CONTEXT_VERSION, "observed_at": observed.isoformat(),
        "watch": {"setup_type": "OI_WALL_RETEST_READY", "direction": direction, "spot": spot, "minimum_move_points": minimum,
                  "closing_threshold": round(spot + (-minimum if direction == "BEARISH" else minimum), 4),
                  "closing_outcome": "last regular-session one-minute spot close strictly beyond threshold in watch direction",
                  "session_close": session_close.isoformat()},
        "wall": wall,
        "price": {"completed_candle_count": len(complete), "contiguous_completed_count": len(contiguous),
                  "latest_source_timestamp": newest.isoformat() if newest else None,
                  "source_age_seconds": (observed-newest).total_seconds() if newest else None,
                  "latest_completed_timestamp": last.isoformat() if last else None,
                  "completed_source_age_seconds": (observed-last).total_seconds() if last else None,
                  "completion_basis": "explicit completed provider sample after bar end, available by observation",
                  "latest_candle": rows[newest] if newest else None, "returns_points": returns,
                  "observed_window_net_points": net,
                  "directional_efficiency": abs(net)/path if path else None,
                  "realized_volatility_points": pstdev(changes)*sqrt(len(changes)) if len(changes)>=2 else None,
                  "atr_14_points": sum(ranges[-14:])/14 if len(ranges)>=14 else None,
                  "recent_candles": [rows[at] for at in ordered[-15:]], "swings": swings,
                  "vwap": None},
        "session_so_far": {"minutes_elapsed": max(0, (observed-session_open).total_seconds()/60),
                           "minutes_remaining": max(0, (session_close-observed).total_seconds()/60),
                           "covers_session_open": covers_session,
                           "open": rows[session_open]["open"] if session_open in rows else None,
                           "high": max([spot]+[r["high"] for r in extrema]) if covers_session else None,
                           "low": min([spot]+[r["low"] for r in extrema]) if covers_session else None,
                           "opening_range_15m": {"high": max(rows[at]["high"] for at in opening_times),
                                                  "low": min(rows[at]["low"] for at in opening_times)} if opening_complete else None},
        "structure": {"previous_day_high": _number(pdh), "previous_day_low": _number(pdl),
                      "nearest_directional_level": nearest, "nearby_levels": sorted(level_rows, key=lambda r: abs(r["price"]-spot))[:6]},
        "options": {"expiry_date": str(expiry_date) if expiry_date else None, "available_strike_count": len(chain),
                    "aggregate_scope": "supplied strike rows only; coverage of the exchange's full chain is not verified",
                    "missing_oi_strike_counts": missing_oi, "invalid_strike_count": invalid_strike_count,
                    "oi_totals": totals, "pcr": totals["pe"]/totals["ce"] if totals["ce"] and totals["pe"] is not None else None,
                    "atm": atm, "opposing_walls": opposing,
                    "opposing_walls_basis": "largest supplied directional OI concentrations; candidates, not detector-qualified walls",
                    "nearby_strikes": sorted(chain,key=lambda r:abs(r["strike"]-spot))[:7],
                    "oi_change_basis": "previous available fetch cycle, not necessarily previous day"},
        "unavailable": {"vwap": "provider index volume/VWAP participation source is not verified",
                        "initial_interaction": "interaction timestamp absent, invalid or outside observation/session" if wall["initial_interaction_timestamp"] is None else None,
                        "candle_completion": "some candles lack completed provider provenance" if any(not r["complete"] for r in rows.values()) else None,
                        "oi_history": "no same-strike intraday OI time series supplied",
                        "previous_close_and_gap": "previous close unavailable",
                        "breadth_vix_futures_news": "not supplied",
                        "session_extrema": None if covers_session else "buffer does not cover session open",
                        "return_windows": [k for k,v in returns.items() if v is None]},
    }
