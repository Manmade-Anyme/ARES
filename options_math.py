import asyncio
import math
from typing import Dict, Any, Optional, Tuple
from models import AresSignal
from config import settings

def calculate_risk_amount(capital: float, risk_pct: float) -> float:
    return (capital * risk_pct) / 100.0

def calculate_points(entry: float, exit: float) -> float:
    return abs(entry - exit)

def translate_to_premium(points: float, delta: float) -> float:
    return points * abs(delta)

def calculate_lots(risk_amt: float, sl_points: float, lot_size: int) -> int:
    if sl_points <= 0 or lot_size <= 0:
        return 0
    return math.floor(risk_amt / (sl_points * lot_size))

def calculate_affordable_lots(capital: float, premium: float, lot_size: int) -> int:
    if premium <= 0 or lot_size <= 0:
        return 0
    return math.floor(capital / (premium * lot_size))

async def fetch_dhan_capital(dhan_client: Any) -> float:
    """
    Fetches the available balance from DhanHQ API.
    """
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None,
            lambda: dhan_client.get_fund_limits()
        )
        if res and res.get("status") == "success":
            data = res.get("data", {})
            # Dhan API response has typo: 'availabelBalance'
            balance = data.get("availabelBalance") or data.get("availableBalance")
            if balance is not None:
                return float(balance)
        print(f"[-] Dhan get_fund_limits failed or returned failure: {res}")
    except Exception as e:
        print(f"[-] Error fetching Dhan fund limits: {e}")
    
    return float(settings.default_capital)

def find_optimal_strike(direction: str, full_chain: list) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[float]]:
    """
    Finds the strike that has delta between 0.45 and 0.55 in the direction.
    If multiple, selects the one with the lowest absolute delta (closest to 0.45).
    
    Returns:
        Tuple[strike, option_type, ltp, delta]
    """
    candidates = []
    
    is_bullish = direction == "BULLISH"
    opt_type = "CE" if is_bullish else "PE"
    
    for row in full_chain:
        strike = row["strike"]
        delta_key = "ce_delta" if is_bullish else "pe_delta"
        ltp_key = "ce_ltp" if is_bullish else "pe_ltp"
        
        delta = row.get(delta_key)
        ltp = row.get(ltp_key)
        
        if delta is not None and ltp is not None:
            abs_delta = abs(delta)
            if 0.45 <= abs_delta <= 0.55:
                candidates.append((strike, ltp, delta, abs_delta))
                
    if not candidates:
        return None, None, None, None
        
    # Sort by absolute delta ascending (to find the lowest absolute delta, closest to 0.45)
    candidates.sort(key=lambda x: x[3])
    best = candidates[0]
    return int(best[0]), opt_type, float(best[1]), float(best[2])

async def process_options_calculation(signal: AresSignal, full_chain: list, dhan_client: Any) -> None:
    """
    Applies the risk-managed option sizing and strike selection logic to a signal.
    """
    # 1. Fetch capital
    cap_val = await fetch_dhan_capital(dhan_client)
    
    # 2. Select option contract based on delta criteria
    strike, opt_type, ltp, delta = find_optimal_strike(signal.direction.value, full_chain)
    
    if strike is not None and opt_type is not None:
        # Update signal strike and type to the near 0.45 delta option
        signal.strike_to_trade = strike
        signal.option_type = opt_type
    else:
        # Fallback to current ATM contract on signal (already populated by detectors)
        delta = 0.50  # fallback delta
        ltp = 100.0  # fallback LTP
        for row in full_chain:
            if row["strike"] == signal.strike_to_trade:
                if signal.option_type == "CE":
                    delta = row.get("ce_delta", 0.50)
                    ltp = row.get("ce_ltp", 100.0)
                else:
                    delta = row.get("pe_delta", -0.50)
                    ltp = row.get("pe_ltp", 100.0)
                break
                
    # 3. Lot Sizing Calculations
    risk_pct = float(settings.risk_per_trade_pct)
    risk_amt = calculate_risk_amount(cap_val, risk_pct)
    
    # Entry zone is a tuple: signal.entry_zone
    entry_mid = sum(signal.entry_zone) / 2.0
    
    # Calculate index SL/TP points
    index_sl_pts = calculate_points(entry_mid, signal.stop_loss)
    index_tp_pts = calculate_points(entry_mid, signal.target_1)
    
    # Translate points to premium movement based on selected delta
    opt_sl_pts = translate_to_premium(index_sl_pts, delta)
    opt_tp_pts = translate_to_premium(index_tp_pts, delta)
    
    # Calculate suggested and affordable lots
    lot_size = int(settings.nifty_lot_size)
    suggested_lots = calculate_lots(risk_amt, opt_sl_pts, lot_size)
    affordable_lots = calculate_affordable_lots(cap_val, ltp, lot_size)
    
    adjusted_lots = min(suggested_lots, affordable_lots)
    
    # Option SL and TP Price
    opt_sl_price = max(1.0, ltp - opt_sl_pts)
    opt_tp_price = ltp + opt_tp_pts
    
    # 4. Save to signal properties using logical naming
    signal.suggested_lots = adjusted_lots
    signal.option_sl = opt_sl_price
    signal.option_target = opt_tp_price
    signal.capital = cap_val
    signal.option_delta = delta
    signal.option_premium = ltp
    signal.risk_pct = risk_pct



