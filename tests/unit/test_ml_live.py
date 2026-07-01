import unittest
from unittest.mock import MagicMock

from ml_signal.live import LiveRunner

class TestMLLiveRemediation(unittest.TestCase):

    def test_parse_option_chain_keys(self):
        runner = LiveRunner()
        
        # Dhan API option chain mock response
        mock_response = {
            "status": "success",
            "data": {
                "oc": {
                    "24000.000000": {
                        "ce": {
                            "last_price": 100.0,
                            "implied_volatility": 12.5,
                            "oi": 100000,
                            "greeks": {
                                "gamma": 0.001,
                                "theta": -10.0,
                                "delta": 0.5,
                                "vega": 10.0
                            }
                        },
                        "pe": {
                            "last_price": 95.0,
                            "implied_volatility": 13.0,
                            "oi": 150000,
                            "greeks": {
                                "gamma": 0.0012,
                                "theta": -8.0,
                                "delta": -0.48,
                                "vega": 9.5
                            }
                        }
                    }
                }
            }
        }
        
        atm_ce, atm_pe = runner._parse_option_chain(mock_response, spot=24000.0)
        
        # Verify correct parsing of IV using 'implied_volatility'
        self.assertEqual(atm_ce["iv"], 12.5)
        self.assertEqual(atm_pe["iv"], 13.0)
        
        # Verify correct parsing of nested Greeks
        self.assertEqual(atm_ce["gamma"], 0.001)
        self.assertEqual(atm_pe["vega"], 9.5)

if __name__ == "__main__":
    unittest.main()
