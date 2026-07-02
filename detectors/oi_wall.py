from typing import Optional, List, Dict, Any, Tuple

from models import OHLCVCandle, AresSignal, SetupType, Direction, ResistanceLevel
from config import settings


class OIWallDetector:
    """
    OIWallDetector identifies "OI Wall Rejection" setups.

    This occurs when the price approaches a strike with a massive accumulation
    of Open Interest (an "OI Wall") and is rejected or bounces off it.

    - CE walls above spot act as resistance. A bearish rejection here signals a PE buy.
    - PE walls below spot act as support. A bullish bounce here signals a CE buy.

    A candidate touch on one candle is not enough on its own: the detector is
    stateful and requires the very next candle to confirm follow-through beyond
    the candidate candle's extreme before emitting a signal. This prevents a
    single shallow graze of the wall from triggering a trade.
    """

    def __init__(self):
        """
        Initialize with no pending candidate setup.
        """
        self.pending_setup: Optional[Dict[str, Any]] = None

    def update(self, spot: float, full_chain: List[Dict[str, Any]], candle: OHLCVCandle, levels: List[ResistanceLevel]) -> Optional[AresSignal]:
        """
        Advance the detector by one candle.

        If a candidate setup is pending from the previous candle, check whether
        this candle confirms it (or let it expire). Otherwise, scan the option
        chain for a new candidate touch on this candle.

        Args:
            spot: The current NIFTY spot price.
            full_chain: The full option chain from OIFetcher.
            candle: The latest closed 1-minute OHLCV candle.
            levels: Current key structural support and resistance levels.

        Returns:
            An AresSignal if a rejection/bounce is confirmed on this candle, otherwise None.
        """
        if self.pending_setup is not None:
            setup = self.pending_setup
            self.pending_setup = None  # Single follow-up candle window only

            if setup["direction"] == Direction.BEARISH:
                confirmed = candle.close < setup["candle1_low"]
            else:
                confirmed = candle.close > setup["candle1_high"]

            if confirmed:
                return self._build_signal(
                    candle=candle,
                    spot=spot,
                    wall=setup["wall"],
                    direction=setup["direction"],
                    option_type=setup["option_type"],
                    levels=levels
                )
            # Not confirmed within the single follow-up candle: setup expires.
            return None

        candidate = self._find_candidate(spot, full_chain, candle)
        if candidate:
            self.pending_setup = candidate

        return None

    def _find_candidate(self, spot: float, full_chain: List[Dict[str, Any]], candle: OHLCVCandle) -> Optional[Dict[str, Any]]:
        """
        Scan the option chain for a nearby OI wall and check if the current candle
        shows a genuine wick-rejection touch (the candidate trigger candle).
        """
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

        candle_range = candle.high - candle.low

        # 3. CE wall rejection candidate (Bearish setup -> buy PE)
        if nearest_ce_wall:
            strike = float(nearest_ce_wall["strike"])
            distance = strike - spot

            approaching = distance < settings.oi_wall_approach_distance
            tested_wall = candle.high >= (strike - settings.oi_wall_test_distance)
            rejected = candle.close < candle.open  # Bearish candle
            upper_wick = candle.high - max(candle.open, candle.close)
            wick_rejection = candle_range > 0 and (upper_wick / candle_range) >= settings.oi_wall_wick_rejection_ratio
            writers_holding = nearest_ce_wall["ce_oi"] >= nearest_ce_wall["ce_oi_prev"]

            if approaching and tested_wall and rejected and wick_rejection and writers_holding:
                return {
                    "wall": nearest_ce_wall,
                    "direction": Direction.BEARISH,
                    "option_type": "PE",
                    "candle1_high": candle.high,
                    "candle1_low": candle.low,
                }

        # 4. PE wall bounce candidate (Bullish setup -> buy CE)
        if nearest_pe_wall:
            strike = float(nearest_pe_wall["strike"])
            distance = spot - strike

            approaching = distance < settings.oi_wall_approach_distance
            tested_wall = candle.low <= (strike + settings.oi_wall_test_distance)
            bounced = candle.close > candle.open  # Bullish candle
            lower_wick = min(candle.open, candle.close) - candle.low
            wick_rejection = candle_range > 0 and (lower_wick / candle_range) >= settings.oi_wall_wick_rejection_ratio
            writers_holding = nearest_pe_wall["pe_oi"] >= nearest_pe_wall["pe_oi_prev"]

            if approaching and tested_wall and bounced and wick_rejection and writers_holding:
                return {
                    "wall": nearest_pe_wall,
                    "direction": Direction.BULLISH,
                    "option_type": "CE",
                    "candle1_high": candle.high,
                    "candle1_low": candle.low,
                }

        return None

    def _evaluate_confidence(
        self,
        candle: OHLCVCandle,
        wall: Dict[str, Any],
        direction: Direction
    ) -> Tuple[str, List[str]]:
        """
        Evaluates setup conditions to determine confidence tier and detailed reasons.
        """
        strike = float(wall["strike"])
        extra_reasons = []
        
        # 1. Wall Magnitude
        oi_val = wall["ce_oi"] if direction == Direction.BEARISH else wall["pe_oi"]
        if oi_val >= 1.5 * settings.oi_wall_min_oi:
            extra_reasons.append("Massive wall size confirms strong barrier (>1.5x min)")
            mag_score = 1
        else:
            mag_score = 0
            
        # 2. Active Defence (OI Growth)
        oi_change_pct = wall["ce_oi_change_pct"] if direction == Direction.BEARISH else wall["pe_oi_change_pct"]
        if oi_change_pct >= 1.5 * settings.oi_wall_min_oi_change_pct:
            extra_reasons.append("Aggressive active defending by option writers (>1.5x min change)")
            growth_score = 1
        else:
            growth_score = 0
            
        # 3. Exact Level Penetration
        if direction == Direction.BEARISH:
            pierced = candle.high >= strike
        else:
            pierced = candle.low <= strike
            
        if pierced:
            extra_reasons.append("Price tested wall deeply / pierced the strike")
            pierce_score = 1
        else:
            pierce_score = 0
            
        # 4. Wick Rejection
        candle_range = candle.high - candle.low
        wick_score = 0
        if candle_range > 2.0:
            if direction == Direction.BEARISH:
                if (candle.high - max(candle.open, candle.close)) >= 0.4 * candle_range:
                    extra_reasons.append("Candle showed heavy overhead supply (long upper wick)")
                    wick_score = 1
            else:
                if (min(candle.open, candle.close) - candle.low) >= 0.4 * candle_range:
                    extra_reasons.append("Candle showed strong absorption/buying tail (long lower wick)")
                    wick_score = 1
                    
        score = mag_score + growth_score + pierce_score + wick_score
        confidence = "HIGH" if score >= 2 else "MEDIUM"
        return confidence, extra_reasons

    def _build_signal(
        self,
        candle: OHLCVCandle,
        spot: float,
        wall: Dict[str, Any],
        direction: Direction,
        option_type: str,
        levels: List[ResistanceLevel]
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
        
        confidence, extra_reasons = self._evaluate_confidence(candle, wall, direction)
        reasons.extend(extra_reasons)
        
        # --- Dynamic Target Selection ---
        target_1 = None
        target_2 = None

        if direction == Direction.BEARISH:
            stop_loss = strike + settings.oi_wall_stop_buffer  # Just beyond the wall
            
            # Find supports below spot
            supports = sorted([lvl.price for lvl in levels if lvl.price < candle.close], reverse=True)
            # Filter out levels within 20 points of entry
            supports = [s for s in supports if abs(s - candle.close) >= 20]
            
            if len(supports) >= 1:
                target_1 = supports[0]
                reasons.append(f"Target 1 set at structural support: {target_1:.2f}")
            if len(supports) >= 2:
                target_2 = supports[1]
                reasons.append(f"Target 2 set at structural support: {target_2:.2f}")
            
            # Fallback to fixed points if levels not found or too close
            if not target_1 or abs(target_1 - candle.close) < 15:
                target_1 = candle.close - settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < 30:
                target_2 = candle.close - settings.target_2_pts
        else:
            stop_loss = strike - settings.oi_wall_stop_buffer  # Just beyond the wall
            
            # Find resistances above spot
            resistances = sorted([lvl.price for lvl in levels if lvl.price > candle.close])
            # Filter out levels within 20 points of entry
            resistances = [r for r in resistances if abs(r - candle.close) >= 20]
            
            if len(resistances) >= 1:
                target_1 = resistances[0]
                reasons.append(f"Target 1 set at structural resistance: {target_1:.2f}")
            if len(resistances) >= 2:
                target_2 = resistances[1]
                reasons.append(f"Target 2 set at structural resistance: {target_2:.2f}")
                
            if not target_1 or abs(target_1 - candle.close) < 15:
                target_1 = candle.close + settings.target_1_pts
            if not target_2 or abs(target_2 - candle.close) < 30:
                target_2 = candle.close + settings.target_2_pts

        # Ensure correct ordering (T1 is closer to entry than T2)
        if direction == Direction.BEARISH and target_1 < target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]
        elif direction == Direction.BULLISH and target_1 > target_2:
            target_1, target_2 = target_2, target_1
            reasons = [r.replace("Target 1", "TEMP").replace("Target 2", "Target 1").replace("TEMP", "Target 2") for r in reasons]

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
            confidence=confidence,
            reasons=reasons,
            timestamp=candle.timestamp,
            strike_to_trade=strike_to_trade,
            option_type=option_type
        )
