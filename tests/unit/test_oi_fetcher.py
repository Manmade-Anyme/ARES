import unittest
from unittest.mock import patch, MagicMock
import asyncio

from fetchers.oi_fetcher import OIFetcher
from models import ATMStrikes

class TestOIFetcherRemediation(unittest.IsolatedAsyncioTestCase):

    @patch("fetchers.oi_fetcher.dhanhq")
    async def test_fetch_chain_iv_parsing(self, mock_dhanhq):
        # Setup mock client
        mock_client = MagicMock()
        mock_dhanhq.return_value = mock_client
        
        # Define mock option chain response using live Dhan API format
        mock_client.option_chain.return_value = {
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
        
        fetcher = OIFetcher()
        atm, full_chain = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")
        
        # Verify that implied_volatility is parsed correctly
        self.assertEqual(atm.ce.iv, 12.5)
        self.assertEqual(atm.pe.iv, 13.0)
        
        # Verify greeks are parsed correctly
        self.assertEqual(atm.ce.gamma, 0.001)
        self.assertEqual(atm.pe.delta, -0.48)

if __name__ == "__main__":
    unittest.main()
