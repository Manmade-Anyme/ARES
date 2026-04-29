import asyncio
import unittest
from fetchers.price_fetcher import PriceFetcher
from dotenv import load_dotenv

load_dotenv()

class TestPriceFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = PriceFetcher()

    def test_fetch_previous_day_ohlc(self):
        """
        Verify that fetch_previous_day_ohlc returns a valid tuple of positive floats.
        This actually calls the external Yahoo Finance endpoint to ensure 
        no network or parsing regressions exist.
        """
        pdh, pdl = asyncio.run(self.fetcher.fetch_previous_day_ohlc())

        
        self.assertIsInstance(pdh, float)
        self.assertIsInstance(pdl, float)
        self.assertGreater(pdh, 0.0)
        self.assertGreater(pdl, 0.0)
        self.assertGreaterEqual(pdh, pdl)

if __name__ == '__main__':
    unittest.main()
