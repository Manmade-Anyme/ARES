import asyncio
from typing import Dict, List, Tuple, Any

from dhanhq import dhanhq

from models import ATMStrikes, OptionRow
from config import settings


class OIFetcher:
    """
    OIFetcher fetches the NIFTY option chain from the Dhan API every cycle (e.g., 60 seconds).
    It tracks the previous cycle's Open Interest (OI) for each strike and side to compute
    the OI change percentage dynamically.
    
    The fetcher returns both the At-The-Money (ATM) context (for breakout/exhaustion rules)
    and the full option chain (for the OIWallDetector).
    """

    def __init__(self):
        """
        Initialize the Dhan API client using settings from config.
        Initialize the previous OI snapshot dictionary.
        """
        self.dhan = dhanhq(
            client_id=settings.dhan_client_id,
            access_token=settings.dhan_access_token
        )
        # Tracks previous cycle OI. Keys format: "<strike>_<type>" (e.g., "24000_CE")
        self._prev_oi_snapshot: Dict[str, int] = {}
        self._cached_expiry: str = None
        self._last_expiry_fetch_date = None

    async def get_nearest_expiry(self) -> str:
        """
        Fetch the nearest expiry date from the Dhan API.
        Caches the result for the current day to avoid redundant API calls.
        """
        from datetime import datetime
        now_date = datetime.now().date()
        
        if self._cached_expiry and self._last_expiry_fetch_date == now_date:
            return self._cached_expiry
            
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.dhan.expiry_list(
                under_security_id=int(settings.security_id),
                under_exchange_segment=settings.exchange_segment
            )
        )
        
        if not response or response.get("status") != "success" or "errorType" in response.get("data", {}) if isinstance(response.get("data"), dict) else False:
            error_msg = "Unknown API error"
            error_type = ""
            
            remarks = response.get("remarks")
            if isinstance(remarks, dict):
                error_msg = remarks.get("error_message") or remarks.get("errorMessage") or error_msg
                error_type = remarks.get("error_type") or remarks.get("errorType") or error_type
            elif isinstance(remarks, str) and remarks:
                error_msg = remarks
                
            if error_msg == "Unknown API error":
                data_field = response.get("data")
                if isinstance(data_field, dict):
                    error_msg = data_field.get("errorMessage") or data_field.get("error_message") or data_field.get("message") or data_field.get("error") or error_msg
                    error_type = data_field.get("errorType") or data_field.get("error_type") or error_type
                elif isinstance(data_field, str) and data_field:
                    error_msg = data_field
                    
            if error_msg == "Unknown API error":
                error_msg = f"Unknown API error. Raw response: {response}"
                
            if error_msg:
                raise ValueError(f"Dhan API Error ({error_type}): {error_msg}")
            raise ValueError(f"Failed to fetch expiry list: {response}")
            
        expirations = response.get("data", {}).get("data", [])
        if not expirations:
            raise ValueError("Empty expiry list returned from Dhan API.")
            
        # The API returns a sorted list, we pick the first one
        nearest = expirations[0]
        self._cached_expiry = nearest
        self._last_expiry_fetch_date = now_date
        return nearest

    async def fetch_chain(self, spot_price: float, expiry: str) -> Tuple[ATMStrikes, List[Dict[str, Any]]]:
        """
        Fetch the current option chain, calculate OI changes, and build the ATM strikes and full chain.
        
        Note: The dhan.option_chain API requires an expiry date. We pass it as a parameter here.
        
        Args:
            spot_price: The current underlying spot price of NIFTY.
            expiry: The current expiry date string in YYYY-MM-DD format.
            
        Returns:
            A tuple containing:
                - An ATMStrikes object for the current spot price.
                - The full option chain as a list of dictionaries.
                
        Raises:
            ValueError: If the ATM strike is not found in the option chain response.
        """
        loop = asyncio.get_running_loop()
        
        # The Dhan API is synchronous, run it in an executor to avoid blocking
        response = await loop.run_in_executor(
            None,
            lambda: self.dhan.option_chain(
                under_security_id=int(settings.security_id),
                under_exchange_segment=settings.exchange_segment,
                expiry=expiry
            )
        )
        
        if not response or "data" not in response:
            raise ValueError("Invalid or empty option chain response from Dhan API")
            
        if response.get("status") == "failure" or "errorType" in response.get("data", {}) if isinstance(response.get("data"), dict) else False:
            error_msg = "Unknown API error"
            error_type = ""
            
            remarks = response.get("remarks")
            if isinstance(remarks, dict):
                error_msg = remarks.get("error_message") or remarks.get("errorMessage") or error_msg
                error_type = remarks.get("error_type") or remarks.get("errorType") or error_type
            elif isinstance(remarks, str) and remarks:
                error_msg = remarks
                
            if error_msg == "Unknown API error":
                data_field = response.get("data")
                if isinstance(data_field, dict):
                    error_msg = data_field.get("errorMessage") or data_field.get("error_message") or data_field.get("message") or data_field.get("error") or error_msg
                    error_type = data_field.get("errorType") or data_field.get("error_type") or error_type
                elif isinstance(data_field, str) and data_field:
                    error_msg = data_field
                    
            if error_msg == "Unknown API error":
                error_msg = f"Unknown API error. Raw response: {response}"
                
            raise ValueError(f"Dhan API Error ({error_type}): {error_msg}")
            
        # The Dhan API response format might put the chain inside "oc" or "data" -> "data" -> "oc"
        if "oc" in response["data"]:
            oc = response["data"]["oc"]
        elif "data" in response["data"] and "oc" in response["data"]["data"]:
            oc = response["data"]["data"]["oc"]
        else:
            raise ValueError(f"Invalid option chain response structure: {response['data'].keys()}")
        
        # Calculate ATM strike using the configured strike interval
        atm_strike = round(spot_price / settings.strike_interval) * settings.strike_interval
        
        full_chain: List[Dict[str, Any]] = []
        atm_ce_row: OptionRow = None
        atm_pe_row: OptionRow = None
        atm_found = False
        
        # Process each strike in the option chain
        for strike_key, data in oc.items():
            try:
                strike_val = float(strike_key)
                strike = int(strike_val)
            except ValueError:
                continue
                
            ce_data = data.get("ce", {})
            pe_data = data.get("pe", {})
            
            # ---------------------------
            # CE Processing
            # ---------------------------
            ce_oi = int(ce_data.get("oi", 0))
            # Dhan API uses 'last_price' in the oc dictionary
            ce_ltp = float(ce_data.get("last_price", 0.0))
            ce_iv = float(ce_data.get("iv", 0.0))
            ce_gamma = float(ce_data.get("gamma", 0.0))
            ce_theta = float(ce_data.get("theta", 0.0))
            
            ce_key = f"{strike}_CE"
            ce_oi_prev = self._prev_oi_snapshot.get(ce_key, ce_oi)
            
            if ce_oi_prev == 0:
                ce_oi_change_pct = 0.0
            else:
                ce_oi_change_pct = ((ce_oi - ce_oi_prev) / ce_oi_prev) * 100.0
                
            # Update snapshot for next cycle
            self._prev_oi_snapshot[ce_key] = ce_oi
            
            # ---------------------------
            # PE Processing
            # ---------------------------
            pe_oi = int(pe_data.get("oi", 0))
            pe_ltp = float(pe_data.get("last_price", 0.0))
            pe_iv = float(pe_data.get("iv", 0.0))
            pe_gamma = float(pe_data.get("gamma", 0.0))
            pe_theta = float(pe_data.get("theta", 0.0))
            
            pe_key = f"{strike}_PE"
            pe_oi_prev = self._prev_oi_snapshot.get(pe_key, pe_oi)
            
            if pe_oi_prev == 0:
                pe_oi_change_pct = 0.0
            else:
                pe_oi_change_pct = ((pe_oi - pe_oi_prev) / pe_oi_prev) * 100.0
                
            # Update snapshot for next cycle
            self._prev_oi_snapshot[pe_key] = pe_oi
            
            # Append to full chain list
            full_chain.append({
                "strike": strike,
                "ce_oi": ce_oi,
                "ce_oi_prev": ce_oi_prev,
                "ce_oi_change_pct": ce_oi_change_pct,
                "ce_ltp": ce_ltp,
                "ce_iv": ce_iv,
                "pe_oi": pe_oi,
                "pe_oi_prev": pe_oi_prev,
                "pe_oi_change_pct": pe_oi_change_pct,
                "pe_ltp": pe_ltp,
                "pe_iv": pe_iv
            })
            
            # Build ATM rows if this is the target strike
            if strike == atm_strike:
                atm_found = True
                atm_ce_row = OptionRow(
                    strike=strike,
                    option_type="CE",
                    ltp=ce_ltp,
                    iv=ce_iv,
                    oi=ce_oi,
                    oi_prev=ce_oi_prev,
                    oi_change_pct=ce_oi_change_pct,
                    gamma=ce_gamma,
                    theta=ce_theta
                )
                atm_pe_row = OptionRow(
                    strike=strike,
                    option_type="PE",
                    ltp=pe_ltp,
                    iv=pe_iv,
                    oi=pe_oi,
                    oi_prev=pe_oi_prev,
                    oi_change_pct=pe_oi_change_pct,
                    gamma=pe_gamma,
                    theta=pe_theta
                )
                
        if not atm_found:
            raise ValueError(f"ATM strike {atm_strike} not found in the option chain response.")
            
        atm_strikes = ATMStrikes(
            spot_price=spot_price,
            ce=atm_ce_row,
            pe=atm_pe_row
        )
        
        return atm_strikes, full_chain
