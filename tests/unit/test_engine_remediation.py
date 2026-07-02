import unittest
from unittest.mock import MagicMock, patch
from collections import deque
from datetime import datetime

from engine import AresEngine
from models import OHLCVCandle, ATMStrikes, AresSignal, SetupType, Direction

class TestEngineRemediation(unittest.TestCase):

    def setUp(self):
        self.engine = AresEngine()

    def _make_candle(self, close, volume=100000):
        candle = MagicMock()
        candle.high = close + 1.0
        candle.low = close - 1.0
        candle.close = close
        candle.open = close
        candle.volume = volume
        return candle

    def _make_signal(self, confidence="MEDIUM", direction=Direction.BULLISH):
        opt_type = "CE" if direction == Direction.BULLISH else "PE"
        return AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=direction,
            trigger_price=24000.0,
            entry_zone=(23990.0, 24010.0),
            stop_loss=23950.0,
            target_1=24100.0,
            target_2=24200.0,
            confidence=confidence,
            reasons=["Reason 1"],
            timestamp=datetime.now(),
            strike_to_trade=24000,
            option_type=opt_type
        )

    def test_speed_filter_suppresses_medium_confidence_in_flat_market(self):
        # 1. Fill candle buffer with flat prices (range of 2 points)
        for i in range(15):
            self.engine.candle_buffer.append(self._make_candle(24000.0 + (i % 2)))
        
        # 2. Mock detectors to return a MEDIUM confidence signal
        mock_signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        
        # Patch the detectors to return this mock signal
        self.engine.breakout_detector.update = MagicMock(return_value=mock_signal)
        
        # 3. Tick the engine
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = 12.0
        atm.pe = MagicMock()
        atm.pe.iv = 12.0
        atm.spot_price = 24000.0
        
        latest_candle = self._make_candle(24001.0)
        signal = self.engine.tick(latest_candle, [], atm, 0.0, [])
        
        # The signal should be suppressed (return None)
        self.assertIsNone(signal)

    def test_speed_filter_allows_medium_confidence_in_trending_market(self):
        # 1. Fill candle buffer with trending prices (range of 30 points)
        for i in range(15):
            self.engine.candle_buffer.append(self._make_candle(24000.0 + i * 2))
            
        mock_signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        self.engine.breakout_detector.update = MagicMock(return_value=mock_signal)
        
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = 12.0
        atm.pe = MagicMock()
        atm.pe.iv = 12.0
        atm.spot_price = 24030.0
        
        latest_candle = self._make_candle(24030.0)
        signal = self.engine.tick(latest_candle, [], atm, 0.0, [])
        
        # The signal should not be suppressed
        self.assertIsNotNone(signal)
        self.assertEqual(signal.confidence, "MEDIUM")

    def test_anti_iv_crush_filter_suppresses_bullish_call_entry(self):
        # 1. Fill IV lookback with low IVs
        if not hasattr(self.engine, "iv_lookback"):
            self.engine.iv_lookback = deque(maxlen=20)
            
        # Clear it first
        self.engine.iv_lookback.clear()
        # Add 18 low elements
        for _ in range(18):
            self.engine.iv_lookback.append(10.0)
        
        # 2. Mock a MEDIUM BULLISH signal (TASK-172: HIGH confidence is exempt
        # from the anti-IV-crush filter, so only MEDIUM gets suppressed)
        mock_signal = self._make_signal(confidence="MEDIUM", direction=Direction.BULLISH)
        self.engine.breakout_detector.update = MagicMock(return_value=mock_signal)
        
        # Fill candle buffer with trending prices so speed filter doesn't trigger
        for i in range(15):
            self.engine.candle_buffer.append(self._make_candle(24000.0 + i * 2))
            
        atm = MagicMock(spec=ATMStrikes)
        atm.ce = MagicMock()
        atm.ce.iv = 15.0 # current high IV
        atm.pe = MagicMock()
        atm.pe.iv = 12.0
        atm.spot_price = 24030.0
        
        latest_candle = self._make_candle(24030.0)
        signal = self.engine.tick(latest_candle, [], atm, 0.0, [])
        
        # The signal should be suppressed because of top 90th percentile IV + BULLISH
        self.assertIsNone(signal)

if __name__ == "__main__":
    unittest.main()
