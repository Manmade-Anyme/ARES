from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from models import OHLCVCandle, AresSignal, SetupType, Direction
from config import settings


class OIWallDetector:
    """
    OIWallDetector identifies "OI Wall Rejection" setups.
    
    This occurs when the price approaches a strike with a massive accumulation
    of Open Interest (an "OI Wall") and is rejected or bounces off it.
    
    - CE walls above spot act as resistance. A bearish rejection here signals a PE buy.
    - PE walls below spot act as support. A bullish bounce here signals a CE buy.
    
    This detector manages its own cooldown so it never blocks or is blocked by
    other detectors.
    """

    def __init__(self):
        self.last_debug: Optional[Dict[str, Any]] = None
        self.last_signal_time: Optional[datetime] = None

    def detect(self, spot: float, full_chain: List[Dict[str, Any]], candle: OHLCVCandle) -> Optional[AresSignal]:
        """
        Scan the option chain for nearby OI walls and check if the current candle
        shows a rejection or bounce confirming the wall's defense.

        Each detector manages its own cooldown independently.
        
        Args:
            spot: The current NIFTY spot price.
            full_chain: The full option chain from OIFetcher.
            candle: The latest closed 1-minute OHLCV candle.
            
        Returns:
            An AresSignal if a rejection/bounce is confirmed, otherwise None.
        """
        # Self-cooldown: skip if this detector fired recently
        if self.last_signal_time:
            elapsed = datetime.now() - self.last_signal_time
            if elapsed < timedelta(minutes=settings.signal_cooldown_minutes):
                return None

        nearest_ce_wall = None
        nearest_pe_wall = None
        
        # 1 & 2: Find the nearest CE and PE walls
        for row in full_chain:
            strike = float(row["strike"])
            
            # CE Walls (Resistance, above spot)
            if strike > spot:
                ce_oi = row["ce_oi"]
                ce_oi_change_pct = row["ce_oi_change_pct"]
                
                if ce_oi > settings.oi_wall_min_oi and ce_oi_change_pct > settings.oi_wall_min_oi_change_pct:
                    # If multiple exist above spot, find the closest one
                    if nearest_ce_wall is None or strike < nearest_ce_wall["strike"]:
                        nearest_ce_wall = row
                        
            # PE Walls (Support, below spot)
            elif strike < spot:
                pe_oi = row["pe_oi"]
                pe_oi_change_pct = row["pe_oi_change_pct"]
                
                if pe_oi > settings.oi_wall_min_oi and pe_oi_change_pct > settings.oi_wall_min_oi_change_pct:
                    # If multiple exist below spot, find the closest one
                    if nearest_pe_wall is None or strike > nearest_pe_wall["strike"]:
                        nearest_pe_wall = row

        debug = dict(ce=None, pe=None)
        
        # 3. CE wall rejection check (Bearish setup -> buy PE)
        if nearest_ce_wall:
            strike = float(nearest_ce_wall["strike"])
            distance = strike - spot
            
            approaching = distance < settings.oi_wall_approach_distance
            tested_wall = candle.high >= (strike - settings.oi_wall_test_distance)
            rejected = candle.close < candle.open  # Bearish candle
            writers_holding = nearest_ce_wall["ce_oi"] >= nearest_ce_wall["ce_oi_prev"] * 0.95
            
            debug["ce"] = dict(
                strike=strike,
                oi=nearest_ce_wall["ce_oi"],
                oi_change_pct=nearest_ce_wall["ce_oi_change_pct"],
                distance_from_spot=distance,
                approaching=approaching,
                tested_wall=tested_wall,
                rejected=rejected,
                writers_holding=writers_holding,
            )
            
            if approaching and tested_wall and rejected and writers_holding:
                self.last_signal_time = datetime.now()
                debug["ce"]["fired"] = True
                self.last_debug = debug
                return self._build_signal(
                    candle=candle,
                    spot=spot,
                    wall=nearest_ce_wall,
                    direction=Direction.BEARISH,
                    option_type="PE"
                )

        # 4. PE wall bounce check (Bullish setup -> buy CE)
        if nearest_pe_wall:
            strike = float(nearest_pe_wall["strike"])
            distance = spot - strike
            
            approaching = distance < settings.oi_wall_approach_distance
            tested_wall = candle.low <= (strike + settings.oi_wall_test_distance)
            bounced = candle.close > candle.open  # Bullish candle
            writers_holding = nearest_pe_wall["pe_oi"] >= nearest_pe_wall["pe_oi_prev"] * 0.95
            
            debug["pe"] = dict(
                strike=strike,
                oi=nearest_pe_wall["pe_oi"],
                oi_change_pct=nearest_pe_wall["pe_oi_change_pct"],
                distance_from_spot=distance,
                approaching=approaching,
                tested_wall=tested_wall,
                bounced=bounced,
                writers_holding=writers_holding,
            )
            
            if approaching and tested_wall and bounced and writers_holding:
                self.last_signal_time = datetime.now()
                debug["pe"]["fired"] = True
                self.last_debug = debug
                return self._build_signal(
                    candle=candle,
                    spot=spot,
                    wall=nearest_pe_wall,
                    direction=Direction.BULLISH,
                    option_type="CE"
                )

        self.last_debug = debug
        return None

    def _build_signal(
        self,
        candle: OHLCVCandle,
        spot: float,
        wall: Dict[str, Any],
        direction: Direction,
        option_type: str
    ) -> AresSignal:
        """
        Constructs the AresSignal for an OI Wall rejection or bounce.
        """
        strike = float(wall["strike"])
        
        # Wall size in lakhs (1 lakh = 100,000)
        oi_val = wall["ce_oi"] if direction == Direction.BEARISH else wall["pe_oi"]
        oi_change_pct = wall["ce_oi_change_pct"] if direction == Direction.BEARISH else wall["pe_oi_change_pct"]
        
        oi_lakhs = oi_val / 100000.0
        
        reasons = [
            f"Price approached massive OI wall at {strike}",
            f"Wall size: {oi_lakhs:.1f}L contracts (+{oi_change_pct:.1f}% change)",
            "Candle showed clear rejection/bounce",
            "Option writers defended the level (OI did not drop)"
        ]
        
        if direction == Direction.BEARISH:
            stop_loss = strike + settings.oi_wall_stop_buffer  # Just beyond the wall
            target_1 = candle.close - settings.target_1_pts
            target_2 = candle.close - settings.target_2_pts
        else:
            stop_loss = strike - settings.oi_wall_stop_buffer  # Just beyond the wall
            target_1 = candle.close + settings.target_1_pts
            target_2 = candle.close + settings.target_2_pts

        entry_zone = (candle.close - settings.entry_zone_offset_pts, candle.close + settings.entry_zone_offset_pts)
        strike_to_trade = int(round(spot / settings.strike_interval) * settings.strike_interval)

        return AresSignal(
            setup_type=SetupType.OI_WALL_REJECTION,
            direction=direction,
            trigger_price=candle.close,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            confidence="HIGH",  # Writers defending a massive wall is inherently high confidence
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type=option_type
        )
