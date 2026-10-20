"""Pure option projections and fail-closed sizing for MANM-176."""
import asyncio
import math
from dataclasses import asdict, replace
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_FLOOR
from typing import Any, Optional, Tuple

from config import settings
from config_profiles import TuningConfig
from models import AresSignal, OptionGreeks, OptionOrderBracket

IST = timezone(timedelta(hours=5, minutes=30))
CAPITAL_SIZING_REJECTIONS = frozenset({'NO_AVAILABLE_CAPITAL', 'INSUFFICIENT_CAPITAL_OR_RISK_BUDGET'})


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_capital(capital: Optional[float]) -> Tuple[float, bool]:
    if not _finite(capital) or capital <= 0:
        return 0.0, False
    return float(capital), True


def calculate_risk_amount(capital: float, risk_pct: float) -> float:
    balance, valid = validate_capital(capital)
    if not valid or not _finite(risk_pct) or not 0 < risk_pct <= 100:
        return 0.0
    return balance * (risk_pct / 100.0)


def calculate_points(entry: float, exit: float) -> float:
    return abs(entry - exit)


def translate_to_premium(points: float, delta: float) -> float:
    """Legacy first-order helper, retained for callers outside bracket pricing."""
    return points * abs(delta)


def calculate_greek_premium_translation(spot_delta: float, delta: float, gamma: float) -> float:
    """Signed first- and second-order change; PE spot moves remain negative."""
    if not all(_finite(value) for value in (spot_delta, delta, gamma)) or gamma < 0:
        raise ValueError('Premium projection requires finite inputs and nonnegative gamma')
    return delta * spot_delta + .5 * gamma * spot_delta * spot_delta


def project_option_premium(spot_delta: float, greeks: OptionGreeks,
                           holding_days: float = 0, iv_change: float = 0) -> float:
    """Premium change; theta is per day, vega per IV percentage-point change."""
    if (not all(_finite(v) for v in (holding_days, iv_change, greeks.theta, greeks.vega))
            or holding_days < 0 or greeks.vega < 0):
        raise ValueError('Scenario inputs must be finite with nonnegative time and vega')
    return (calculate_greek_premium_translation(spot_delta, greeks.delta, greeks.gamma)
            + greeks.theta * holding_days + greeks.vega * iv_change)


def round_to_tick(price: float, tick_size: float = .05) -> float:
    if not _finite(price) or not _finite(tick_size) or tick_size <= 0:
        raise ValueError('Price and positive tick size must be finite')
    try:
        ticks = (Decimal(str(price)) / Decimal(str(tick_size))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError('Price exceeds supported tick precision') from exc
    return float(ticks * Decimal(str(tick_size)))


def _lots(numerator: float, points: float, lot_size: int) -> int:
    if (not all(_finite(v) and v > 0 for v in (numerator, points, lot_size))
            or int(lot_size) != lot_size):
        return 0
    ratio = numerator / points / lot_size
    return max(0, math.floor(ratio)) if math.isfinite(ratio) else 0


def calculate_lots(risk_amt: float, sl_points: float, lot_size: int) -> int:
    return _lots(risk_amt, sl_points, lot_size)


def calculate_affordable_lots(capital: float, premium: float, lot_size: int) -> int:
    return _lots(capital, premium, lot_size)


async def fetch_dhan_capital(dhan_client: Any) -> Optional[float]:
    """Return actual finite balance, preserving zero; never use an unverified default."""
    try:
        response = await asyncio.to_thread(dhan_client.get_fund_limits)
        if not isinstance(response, dict) or response.get('status') != 'success':
            return None
        data = response.get('data')
        if not isinstance(data, dict):
            return None
        balance = data.get('availabelBalance')
        if balance is None:
            balance = data.get('availableBalance')
        if balance is None or isinstance(balance, bool):
            return None
        balance = float(balance)
        return balance if math.isfinite(balance) else None
    except Exception:
        # A broker failure is unavailable capital, not permission to trade a default account.
        return None


def _expiry(row: dict) -> Optional[date]:
    try:
        return date.fromisoformat(row['expiry_date'])
    except (KeyError, TypeError, ValueError):
        return None


def _select_contract(direction: str, full_chain: list, as_of: Optional[date] = None):
    if direction not in ('BULLISH', 'BEARISH') or not isinstance(full_chain, list):
        return None
    timestamp = as_of if isinstance(as_of, datetime) else None
    if timestamp is not None:
        timestamp = timestamp.replace(tzinfo=IST) if timestamp.tzinfo is None else timestamp.astimezone(IST)
        as_of = timestamp.date()
    rows = [row for row in full_chain if isinstance(row, dict) and _expiry(row) is not None
            and (as_of is None or _expiry(row) >= as_of)]
    if not rows:
        return None
    # Select expiry before validating quotes: missing current-week quotes must not
    # silently switch the strategy into a less liquid later expiry.
    expiry = min(_expiry(row) for row in rows)
    if timestamp is not None and timestamp >= datetime.combine(expiry, time(15, 30), tzinfo=IST):
        return None
    side = 'ce' if direction == 'BULLISH' else 'pe'
    candidates = []
    for row in rows:
        if _expiry(row) != expiry:
            continue
        values = [row.get(f'{side}_{name}') for name in ('delta', 'gamma', 'theta', 'vega', 'iv')]
        premium, strike = row.get(f'{side}_ltp'), row.get('strike')
        security_id = row.get(f'{side}_security_id')
        if (not all(_finite(value) for value in values)
                or not _finite(premium) or premium <= 0
                or not _finite(strike) or strike <= 0 or int(strike) != strike
                or not isinstance(security_id, (str, int)) or isinstance(security_id, bool)
                or not str(security_id).isdigit() or int(security_id) <= 0):
            continue
        delta, gamma, theta, vega, iv = values
        if (not .45 <= abs(delta) <= 1 or (side == 'ce') != (delta > 0)
                or gamma < 0 or vega < 0 or iv <= 0):
            continue
        candidates.append((abs(abs(delta) - .5), int(strike), row, OptionGreeks(*values)))
    if not candidates:
        return None
    _, strike, row, greeks = min(candidates, key=lambda item: (item[0], item[1]))
    return row, strike, side.upper(), float(row[f'{side}_ltp']), greeks, str(row[f'{side}_security_id'])


def find_optimal_strike(direction: str, full_chain: list):
    """Return the complete current-expiry contract nearest absolute delta 0.50."""
    selected = _select_contract(direction, full_chain)
    return selected[1:] if selected else (None, None, None, None, None)


def _straddle_expected_move(full_chain: list, spot: float, expiry: str) -> Optional[float]:
    pairs = [row for row in full_chain if isinstance(row, dict) and row.get('expiry_date') == expiry
             and all(_finite(row.get(k)) and row[k] > 0 for k in ('strike', 'ce_ltp', 'pe_ltp'))]
    if not pairs:
        return None
    atm = min(pairs, key=lambda row: abs(row['strike'] - spot))
    expected = (atm['ce_ltp'] + atm['pe_ltp']) * 1.25
    return expected if math.isfinite(expected) else None


def _expected_move_diagnostics(signal: AresSignal, full_chain: list) -> dict:
    """Diagnostic only: IV uses calendar time to the expiry close, never a pricing fallback."""
    if not isinstance(full_chain, list):
        return {}
    timestamp = signal.timestamp
    timestamp = timestamp.replace(tzinfo=IST) if timestamp.tzinfo is None else timestamp.astimezone(IST)
    rows = [row for row in full_chain if isinstance(row, dict) and _expiry(row) is not None
            and _expiry(row) >= timestamp.date()]
    if not rows or not _finite(signal.trigger_price) or signal.trigger_price <= 0:
        return {}
    expiry = min(_expiry(row) for row in rows)
    expiry_rows = [row for row in rows if _expiry(row) == expiry
                   and _finite(row.get('strike')) and row['strike'] > 0]
    if not expiry_rows:
        return {}
    atm = min(expiry_rows, key=lambda row: abs(row['strike'] - signal.trigger_price))
    spot = atm.get('underlying_spot', signal.trigger_price)
    if not _finite(spot) or spot <= 0:
        return {}
    straddle = _straddle_expected_move(rows, spot, expiry.isoformat())
    seconds = (datetime.combine(expiry, time(15, 30), tzinfo=IST) - timestamp).total_seconds()
    side = 'ce' if signal.direction.value == 'BULLISH' else 'pe'
    iv = atm.get(f'{side}_iv')
    iv_move = (spot * (iv / 100) * math.sqrt(seconds / (365 * 86400))
               if _finite(iv) and iv > 0 and seconds > 0 else None)
    if iv_move is not None and not _finite(iv_move):
        iv_move = None
    warning = ('IV_FEED_STALE' if straddle and iv_move is not None
               and abs(iv_move - straddle) / straddle > .25 else None)
    return {'expected_move_straddle': straddle, 'expected_move_iv': iv_move, 'iv_feed_warning': warning}


def build_option_bracket(signal: AresSignal, full_chain: list, capital: Optional[float],
                         config: TuningConfig) -> OptionOrderBracket:
    """Price independently of capital; only positive allocations permit execution."""
    balance, capital_valid = validate_capital(capital)
    bracket = OptionOrderBracket('', '', 0, signal.option_type, 0, 0, 0, 0, None,
                                 0, None, False, signal_id=signal.id, capital=balance)
    bracket = replace(bracket, **_expected_move_diagnostics(signal, full_chain))
    lot_size = config.nifty_lot_size
    cap = config.options_max_lots_cap
    buffer = config.options_slippage_buffer_pts
    horizon = config.options_holding_horizon_minutes
    if (not all(_finite(v) for v in (lot_size, cap, buffer, horizon, config.risk_per_trade_pct))
            or lot_size <= 0 or int(lot_size) != lot_size or cap < 0 or int(cap) != cap
            or buffer < 0 or horizon < 0 or not 0 < config.risk_per_trade_pct <= 100):
        return replace(bracket, rejection_reason='INVALID_SIZING_CONFIG')
    selected = _select_contract(signal.direction.value, full_chain, signal.timestamp)
    if selected is None:
        return replace(bracket, rejection_reason='INVALID_DATA')
    row, strike, option_type, premium, greeks, security_id = selected
    spot = row.get('underlying_spot', signal.trigger_price)
    levels = (spot, signal.trigger_price, signal.stop_loss, signal.target_1, signal.target_2)
    if not all(_finite(value) and value > 0 for value in levels):
        return replace(bracket, rejection_reason='INVALID_DATA')
    bullish = option_type == 'CE'
    geometry = (signal.stop_loss < signal.trigger_price < signal.target_1 < signal.target_2
                if bullish else signal.stop_loss > signal.trigger_price > signal.target_1 > signal.target_2)
    if not geometry:
        return replace(bracket, rejection_reason='INVALID_BRACKET_ORDERING')
    side = option_type.lower()
    ask, bid = row.get(f'{side}_ask'), row.get(f'{side}_bid')
    # A missing ask can use the documented LTP cap, but a crossed or malformed
    # book must never lower the risk denominator or fabricate a fill price.
    if ((ask is not None and (not _finite(ask) or ask <= 0))
            or (bid is not None and (not _finite(bid) or bid <= 0))
            or (ask is not None and bid is not None and bid > ask)):
        return replace(bracket, rejection_reason='INVALID_DATA')
    entry_raw = min(ask, premium + buffer) if ask is not None else premium + buffer
    spread = ask - bid if ask is not None and bid is not None else buffer
    projected = [premium + calculate_greek_premium_translation(level - spot, greeks.delta, greeks.gamma)
                 for level in (signal.stop_loss, signal.target_1, signal.target_2)]
    if not all(_finite(value) for value in (entry_raw, spread, *projected)):
        return replace(bracket, rejection_reason='INVALID_DATA')
    try:
        entry, sl, t1, t2 = [round_to_tick(value) for value in (entry_raw, *projected)]
        # Buy limits may never round above the ask/slippage ceiling.
        if entry > entry_raw:
            entry = float((Decimal(str(entry_raw)) / Decimal('.05')).quantize(
                Decimal('1'), rounding=ROUND_FLOOR) * Decimal('.05'))
    except ValueError:
        return replace(bracket, rejection_reason='INVALID_DATA')
    bracket = replace(bracket, security_id=security_id, expiry_date=row['expiry_date'],
                      strike=strike, option_type=option_type, entry_limit=entry,
                      synthetic_spot_sl=signal.stop_loss, catastrophic_opt_sl=sl,
                      target_1=t1, target_2=t2, greeks=greeks, lot_size=int(lot_size),
                      spot_entry=signal.trigger_price, spot_target_1=signal.target_1,
                      spot_target_2=signal.target_2, reference_premium=premium,
                      expected_move_straddle=_straddle_expected_move(full_chain, spot, row['expiry_date']))
    if not .05 <= sl < entry < t1 < t2:
        return replace(bracket, rejection_reason='INVALID_BRACKET_ORDERING')
    # The theoretical gamma benefit may never enlarge allocation. Include the
    # entry premium uplift and adverse executable exit spread before sizing.
    risk_loss = max(abs(greeks.delta) * abs(spot - signal.stop_loss), entry - sl + spread)
    projected_t1 = premium + project_option_premium(signal.target_1 - spot, greeks, horizon / 375.0)
    bracket = replace(bracket, risk_loss_pts=risk_loss, projected_target_1=projected_t1)
    if not _finite(projected_t1) or projected_t1 - entry - spread < risk_loss:
        return replace(bracket, rejection_reason='INSUFFICIENT_OPTION_RR')
    suggested = calculate_lots(calculate_risk_amount(balance, config.risk_per_trade_pct), risk_loss, lot_size)
    affordable = calculate_affordable_lots(balance, entry, lot_size)
    lots = max(0, min(suggested, affordable, int(cap)))
    return replace(bracket, lots=lots, uncapped_suggested_lots=suggested,
                   affordable_lots=affordable, is_valid=lots > 0,
                   rejection_reason=(None if lots > 0 else 'NO_AVAILABLE_CAPITAL' if not capital_valid
                                     else 'INSUFFICIENT_CAPITAL_OR_RISK_BUDGET'))


def option_sizing_context(signal: AresSignal) -> dict:
    """JSON-safe snapshot; legacy suggested_lots always means final allocation."""
    bracket = signal.bracket
    if bracket is None:
        lots = signal.suggested_lots
        lots = int(lots) if _finite(lots) and lots >= 0 and int(lots) == lots else 0
        return {'status': signal.options_sizing_status or 'PENDING', 'suggested_lots': lots,
                'capital': signal.capital, 'delta': signal.option_delta, 'premium': signal.option_premium,
                'option_sl': signal.option_sl, 'option_target': signal.option_target, 'risk_pct': signal.risk_pct}
    return {
        'status': signal.options_sizing_status, 'is_valid': bracket.is_valid,
        'security_id': bracket.security_id, 'expiry_date': bracket.expiry_date,
        'strike': bracket.strike, 'option_type': bracket.option_type,
        'entry_limit': bracket.entry_limit, 'option_premium': bracket.reference_premium,
        'premium': bracket.reference_premium, 'delta': bracket.greeks.delta if bracket.greeks else None,
        'option_sl': bracket.catastrophic_opt_sl, 'synthetic_spot_sl': bracket.synthetic_spot_sl,
        'option_target': bracket.target_1, 'target_1': bracket.target_1, 'target_2': bracket.target_2,
        'suggested_lots': bracket.lots, 'lots': bracket.lots, 'final_lots': bracket.lots,
        'uncapped_suggested_lots': bracket.uncapped_suggested_lots,
        'affordable_lots': bracket.affordable_lots, 'lot_size': bracket.lot_size,
        'capital': bracket.capital, 'risk_pct': signal.risk_pct,
        'risk_loss_pts': bracket.risk_loss_pts, 'option_delta': bracket.greeks.delta if bracket.greeks else None,
        'greeks': asdict(bracket.greeks) if bracket.greeks else None,
        'projected_target_1': bracket.projected_target_1,
        'expected_move_straddle': bracket.expected_move_straddle,
        'expected_move_iv': bracket.expected_move_iv, 'iv_feed_warning': bracket.iv_feed_warning,
        'rejection_reason': bracket.rejection_reason,
    }


def option_sizing_summary(signal: AresSignal) -> str:
    bracket = signal.bracket
    if bracket is None:
        context = option_sizing_context(signal)
        return f"Option Sizing: {context['suggested_lots']} lots | {context['status']}"
    if not bracket.is_valid and bracket.rejection_reason not in CAPITAL_SIZING_REJECTIONS:
        return f'Option sizing unavailable: {bracket.rejection_reason}'
    return (f'Option Sizing: {bracket.lots} lots | {bracket.strike} {bracket.option_type} | '
            f'Limit {bracket.entry_limit:.2f} | Emergency SL {bracket.catastrophic_opt_sl:.2f} | '
            f'T1 {bracket.target_1:.2f} | T2 {bracket.target_2:.2f}')


def attach_options_sizing(signal: AresSignal, bracket: OptionOrderBracket, config: TuningConfig) -> None:
    signal.bracket = bracket
    signal.options_sizing_status = ('VALID' if bracket.is_valid else
                                    bracket.rejection_reason if bracket.rejection_reason in CAPITAL_SIZING_REJECTIONS
                                    else 'INVALID_DATA')
    signal.suggested_lots = bracket.lots
    signal.capital = bracket.capital
    signal.risk_pct = config.risk_per_trade_pct if _finite(config.risk_per_trade_pct) else None
    signal.option_premium = bracket.reference_premium
    signal.option_delta = bracket.greeks.delta if bracket.greeks else None
    signal.option_sl = bracket.catastrophic_opt_sl if bracket.greeks else None
    signal.option_target = bracket.target_1 if bracket.greeks else None
    if bracket.strike:
        signal.strike_to_trade = bracket.strike
        signal.option_type = bracket.option_type
    signal.market_context['options_sizing'] = option_sizing_context(signal)


async def process_options_calculation(signal: AresSignal, full_chain: list, dhan_client: Any,
                                      config: Optional[TuningConfig] = None) -> None:
    active_config = config if config is not None else settings
    capital = await fetch_dhan_capital(dhan_client)
    attach_options_sizing(signal, build_option_bracket(signal, full_chain, capital, active_config), active_config)
