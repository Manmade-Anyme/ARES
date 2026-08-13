import unittest
from unittest.mock import MagicMock, patch
import asyncio
from models import AresSignal, SetupType, Direction
from datetime import datetime
from options_math import (
    calculate_risk_amount,
    calculate_points,
    translate_to_premium,
    calculate_lots,
    calculate_affordable_lots,
    find_optimal_strike,
    process_options_calculation,
    fetch_dhan_capital
)

class TestOptionsMath(unittest.IsolatedAsyncioTestCase):
    def test_risk_amount(self):
        self.assertEqual(calculate_risk_amount(200000.0, 1.5), 3000.0)
        self.assertAlmostEqual(calculate_risk_amount(9106.09, 10.0), 910.609, places=4)

    def test_points(self):
        self.assertEqual(calculate_points(24087.30, 24115.05), 27.75)
        self.assertEqual(calculate_points(24087.30, 24017.30), 70.0)

    def test_translate_to_premium(self):
        self.assertAlmostEqual(translate_to_premium(27.75, -0.46), 12.765)

    def test_calculate_lots(self):
        # Risk = 910.61, Option SL pts = 12.765, lot size = 65 -> 910.61 / (12.765 * 65) = 1.09 -> 1 lot
        self.assertEqual(calculate_lots(910.61, 12.765, 65), 1)
        self.assertEqual(calculate_lots(2000.0, 10.0, 65), 3)

    def test_calculate_lots_zero(self):
        self.assertEqual(calculate_lots(100.0, 0.0, 65), 0)
        self.assertEqual(calculate_lots(100.0, 10.0, 0), 0)

    def test_calculate_affordable_lots(self):
        self.assertEqual(calculate_affordable_lots(9106.09, 105.4, 65), 1)

    def test_calculate_affordable_lots_zero(self):
        self.assertEqual(calculate_affordable_lots(1000.0, 0.0, 65), 0)
        self.assertEqual(calculate_affordable_lots(1000.0, 10.0, 0), 0)

    def test_find_optimal_strike_bullish(self):
        full_chain = [
            {"strike": 24000, "ce_delta": 0.5967, "ce_ltp": 160.95, "pe_delta": -0.38021, "pe_ltp": 64.5},
            {"strike": 24050, "ce_delta": 0.53292, "ce_ltp": 129.15, "pe_delta": -0.45982, "pe_ltp": 82.8},
            {"strike": 24100, "ce_delta": 0.4647, "ce_ltp": 102.45, "pe_delta": -0.54597, "pe_ltp": 105.4},
        ]
        # For BULLISH, CE delta absolute value between 0.45 and 0.55.
        # Candidates: 24050 (0.53292) and 24100 (0.4647).
        # Lowest absolute delta is 0.4647 (strike 24100).
        strike, opt_type, ltp, delta = find_optimal_strike("BULLISH", full_chain)
        self.assertEqual(strike, 24100)
        self.assertEqual(opt_type, "CE")
        self.assertEqual(ltp, 102.45)
        self.assertEqual(delta, 0.4647)

    def test_find_optimal_strike_bearish(self):
        full_chain = [
            {"strike": 24000, "ce_delta": 0.5967, "ce_ltp": 160.95, "pe_delta": -0.38021, "pe_ltp": 64.5},
            {"strike": 24050, "ce_delta": 0.53292, "ce_ltp": 129.15, "pe_delta": -0.45982, "pe_ltp": 82.8},
            {"strike": 24100, "ce_delta": 0.4647, "ce_ltp": 102.45, "pe_delta": -0.54597, "pe_ltp": 105.4},
        ]
        # For BEARISH, PE delta absolute value between 0.45 and 0.55.
        # Candidates: 24050 (abs delta 0.45982) and 24100 (abs delta 0.54597).
        # Lowest absolute delta is 0.45982 (strike 24050).
        strike, opt_type, ltp, delta = find_optimal_strike("BEARISH", full_chain)
        self.assertEqual(strike, 24050)
        self.assertEqual(opt_type, "PE")
        self.assertEqual(ltp, 82.8)
        self.assertEqual(delta, -0.45982)

    def test_find_optimal_strike_none(self):
        full_chain = [
            {"strike": 24000, "ce_delta": 0.8, "ce_ltp": 160.95, "pe_delta": -0.2, "pe_ltp": 64.5},
        ]
        strike, opt_type, ltp, delta = find_optimal_strike("BULLISH", full_chain)
        self.assertIsNone(strike)

    @patch('options_math.settings')
    async def test_fetch_dhan_capital_success(self, mock_settings):
        dhan_client = MagicMock()
        dhan_client.get_fund_limits.return_value = {
            "status": "success",
            "data": {
                "availabelBalance": 15000.50
            }
        }
        val = await fetch_dhan_capital(dhan_client)
        self.assertEqual(val, 15000.50)

        # check fallback 'availableBalance'
        dhan_client.get_fund_limits.return_value = {
            "status": "success",
            "data": {
                "availableBalance": 20000.00
            }
        }
        val = await fetch_dhan_capital(dhan_client)
        self.assertEqual(val, 20000.00)

    @patch('options_math.settings')
    async def test_fetch_dhan_capital_failure(self, mock_settings):
        mock_settings.default_capital = 10000.00
        dhan_client = MagicMock()
        dhan_client.get_fund_limits.return_value = {
            "status": "failure"
        }
        val = await fetch_dhan_capital(dhan_client)
        self.assertEqual(val, 10000.00)

    @patch('options_math.settings')
    async def test_fetch_dhan_capital_exception(self, mock_settings):
        mock_settings.default_capital = 10000.00
        dhan_client = MagicMock()
        dhan_client.get_fund_limits.side_effect = Exception("API connection error")
        val = await fetch_dhan_capital(dhan_client)
        self.assertEqual(val, 10000.00)

    @patch('options_math.fetch_dhan_capital')
    async def test_process_options_calculation(self, mock_fetch_capital):
        mock_fetch_capital.return_value = 22765.225
        
        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BEARISH,
            trigger_price=24087.30,
            entry_zone=(24082.30, 24092.30),
            stop_loss=24115.05,
            target_1=24017.30,
            target_2=23789.25,
            confidence="MEDIUM",
            reasons=["Mock reason"],
            timestamp=datetime.now(),
            strike_to_trade=24100,
            option_type="PE"
        )
        
        full_chain = [
            {"strike": 24050, "ce_delta": 0.53292, "ce_ltp": 129.15, "pe_delta": -0.45982, "pe_ltp": 82.8},
            {"strike": 24100, "ce_delta": 0.4647, "ce_ltp": 102.45, "pe_delta": -0.54597, "pe_ltp": 105.4},
        ]
        
        await process_options_calculation(signal, full_chain, MagicMock())
        
        # Verify signal properties updated
        self.assertEqual(signal.strike_to_trade, 24050)
        self.assertEqual(signal.option_type, "PE")
        self.assertEqual(signal.capital, 22765.225)
        self.assertEqual(signal.option_premium, 82.8)
        self.assertEqual(signal.option_delta, -0.45982)
        self.assertAlmostEqual(signal.option_sl, 82.8 - (27.75 * 0.45982), places=4)
        self.assertAlmostEqual(signal.option_target, 82.8 + (70.0 * 0.45982), places=4)
        self.assertEqual(signal.suggested_lots, 1)
        self.assertFalse(any("Option Sizing" in r for r in signal.reasons))

    @patch('options_math.fetch_dhan_capital')
    async def test_process_options_calculation_fallback(self, mock_fetch_capital):
        mock_fetch_capital.return_value = 10000.00
        
        # Scenario where no options in the chain match optimal delta criteria
        # So it should fallback to strike_to_trade on the signal (24100 PE)
        signal = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BEARISH,
            trigger_price=24087.30,
            entry_zone=(24082.30, 24092.30),
            stop_loss=24115.05,
            target_1=24017.30,
            target_2=23789.25,
            confidence="MEDIUM",
            reasons=["Mock reason"],
            timestamp=datetime.now(),
            strike_to_trade=24100,
            option_type="PE"
        )
        
        # All deltas outside [0.45, 0.55]
        full_chain = [
            {"strike": 24100, "ce_delta": 0.80, "ce_ltp": 150.0, "pe_delta": -0.20, "pe_ltp": 50.0},
        ]
        
        await process_options_calculation(signal, full_chain, MagicMock())
        self.assertEqual(signal.strike_to_trade, 24100)
        self.assertEqual(signal.option_type, "PE")
        self.assertEqual(signal.option_delta, -0.20)
        self.assertEqual(signal.option_premium, 50.0)

        # Test fallback for option_type == "CE"
        signal_ce = AresSignal(
            setup_type=SetupType.FAILED_BREAKOUT,
            direction=Direction.BULLISH,
            trigger_price=24087.30,
            entry_zone=(24082.30, 24092.30),
            stop_loss=24050.0,
            target_1=24150.0,
            target_2=24200.0,
            confidence="MEDIUM",
            reasons=["Mock reason"],
            timestamp=datetime.now(),
            strike_to_trade=24100,
            option_type="CE"
        )
        await process_options_calculation(signal_ce, full_chain, MagicMock())
        self.assertEqual(signal_ce.strike_to_trade, 24100)
        self.assertEqual(signal_ce.option_type, "CE")
        self.assertEqual(signal_ce.option_delta, 0.80)
        self.assertEqual(signal_ce.option_premium, 150.0)

if __name__ == '__main__':
    unittest.main()
