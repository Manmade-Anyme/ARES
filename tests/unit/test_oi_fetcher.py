import unittest
from unittest.mock import patch, MagicMock

from fetchers.oi_fetcher import OIFetcher

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

    @patch("fetchers.oi_fetcher.dhanhq")
    async def test_fetch_chain_preserves_none_for_missing_fields(self, mock_dhanhq):
        """Verify that OIFetcher.fetch_chain preserves None when upstream fields are omitted."""
        mock_client = MagicMock()
        mock_dhanhq.return_value = mock_client

        # Partial option chain: CE has only last_price and oi; PE has only last_price and iv
        mock_client.option_chain.return_value = {
            "status": "success",
            "data": {
                "oc": {
                    "24000.000000": {
                        "ce": {
                            "last_price": 100.0,
                            "oi": 100000,
                        },
                        "pe": {
                            "last_price": 95.0,
                            "implied_volatility": 13.0,
                        }
                    }
                }
            }
        }

        fetcher = OIFetcher()
        atm, full_chain = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")

        # CE checks: OI and LTP present, IV and Greeks missing -> None
        self.assertEqual(atm.ce.oi, 100000)
        self.assertEqual(atm.ce.ltp, 100.0)
        self.assertIsNone(atm.ce.iv)
        self.assertIsNone(atm.ce.gamma)
        self.assertIsNone(atm.ce.theta)
        self.assertIsNone(atm.ce.delta)
        self.assertIsNone(atm.ce.vega)

        # PE checks: IV and LTP present, OI and Greeks missing -> None
        self.assertEqual(atm.pe.iv, 13.0)
        self.assertEqual(atm.pe.ltp, 95.0)
        self.assertIsNone(atm.pe.oi)
        self.assertIsNone(atm.pe.oi_prev)
        self.assertIsNone(atm.pe.oi_change_pct)
        self.assertIsNone(atm.pe.gamma)
        self.assertIsNone(atm.pe.delta)

        # Verify full_chain contains None for omitted fields
        row = full_chain[0]
        self.assertIsNone(row["ce_iv"])
        self.assertIsNone(row["pe_oi"])
        self.assertIsNone(row["pe_delta"])

    def test_parse_helpers(self):
        """Verify _parse_float and _parse_int handle edge cases gracefully."""
        from fetchers.oi_fetcher import _parse_float, _parse_int

        self.assertIsNone(_parse_float(None))
        self.assertIsNone(_parse_float("invalid"))
        self.assertEqual(_parse_float(12.5), 12.5)
        self.assertEqual(_parse_float("14.25"), 14.25)

        self.assertIsNone(_parse_int(None))
        self.assertIsNone(_parse_int("invalid"))
        self.assertEqual(_parse_int(100), 100)
        self.assertEqual(_parse_int("250"), 250)

    @patch("fetchers.oi_fetcher.dhanhq")
    async def test_fetch_chain_zero_prev_oi_and_missing_ce_oi(self, mock_dhanhq):
        """Verify fetch_chain handles zero previous OI without dividing by zero and missing CE OI."""
        mock_client = MagicMock()
        mock_dhanhq.return_value = mock_client

        mock_client.option_chain.return_value = {
            "status": "success",
            "data": {
                "oc": {
                    "24000.000000": {
                        "ce": {
                            "last_price": 100.0,
                            # ce oi omitted entirely
                        },
                        "pe": {
                            "last_price": 95.0,
                            "oi": 5000,
                        }
                    }
                }
            }
        }

        fetcher = OIFetcher()
        # Seed snapshot with 0 for PE to test zero previous OI branch
        fetcher._prev_oi_snapshot["24000_PE"] = 0

        atm, _ = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")

        # CE has no OI -> None
        self.assertIsNone(atm.ce.oi)
        self.assertIsNone(atm.ce.oi_prev)
        self.assertIsNone(atm.ce.oi_change_pct)

        # PE previous was 0 -> change pct is 0.0 without ZeroDivisionError
        self.assertEqual(atm.pe.oi, 5000)
        self.assertEqual(atm.pe.oi_prev, 0)
        self.assertEqual(atm.pe.oi_change_pct, 0.0)

        # Now test CE zero previous OI branch
        mock_client.option_chain.return_value["data"]["oc"]["24000.000000"]["ce"]["oi"] = 4000
        fetcher._prev_oi_snapshot["24000_CE"] = 0
        atm2, _ = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")
        self.assertEqual(atm2.ce.oi, 4000)
        self.assertEqual(atm2.ce.oi_prev, 0)
        self.assertEqual(atm2.ce.oi_change_pct, 0.0)

    @patch("fetchers.oi_fetcher.dhanhq")
    async def test_fetch_chain_invalidates_stale_oi_snapshot_after_gap(self, mock_dhanhq):
        """Verify that a missing OI reading invalidates the snapshot so the first post-gap change is unknown."""
        mock_client = MagicMock()
        mock_dhanhq.return_value = mock_client
        fetcher = OIFetcher()

        # Cycle 1: valid OI = 1000
        mock_client.option_chain.return_value = {
            "status": "success",
            "data": {
                "oc": {
                    "24000.000000": {
                        "ce": {"last_price": 100.0, "oi": 1000},
                        "pe": {"last_price": 95.0, "oi": 1000}
                    }
                }
            }
        }
        atm1, _ = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")
        self.assertEqual(atm1.ce.oi, 1000)
        self.assertIsNone(atm1.ce.oi_prev)
        self.assertIsNone(atm1.ce.oi_change_pct)
        self.assertEqual(fetcher._prev_oi_snapshot["24000_CE"], 1000)

        # Cycle 2: gap (ce oi is omitted from payload)
        mock_client.option_chain.return_value["data"]["oc"]["24000.000000"]["ce"] = {"last_price": 105.0}
        atm2, _ = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")
        self.assertIsNone(atm2.ce.oi)
        self.assertIsNone(atm2.ce.oi_prev)
        self.assertIsNone(atm2.ce.oi_change_pct)
        # Snapshot for 24000_CE must be invalidated/removed
        self.assertNotIn("24000_CE", fetcher._prev_oi_snapshot)

        # Cycle 3: post-gap recovery with oi = 1500
        mock_client.option_chain.return_value["data"]["oc"]["24000.000000"]["ce"] = {
            "last_price": 110.0,
            "oi": 1500
        }
        atm3, _ = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")
        self.assertEqual(atm3.ce.oi, 1500)
        # Baseline was invalidated during gap, so change is unknown (None), NOT a stale 50% jump!
        self.assertIsNone(atm3.ce.oi_prev)
        self.assertIsNone(atm3.ce.oi_change_pct)
        self.assertEqual(fetcher._prev_oi_snapshot["24000_CE"], 1500)

        # Cycle 4: subsequent regular cycle with oi = 1650
        mock_client.option_chain.return_value["data"]["oc"]["24000.000000"]["ce"] = {
            "last_price": 115.0,
            "oi": 1650
        }
        atm4, _ = await fetcher.fetch_chain(spot_price=24000.0, expiry="2026-07-07")
        self.assertEqual(atm4.ce.oi, 1650)
        self.assertEqual(atm4.ce.oi_prev, 1500)
        self.assertEqual(atm4.ce.oi_change_pct, 10.0)


if __name__ == "__main__":
    unittest.main()
