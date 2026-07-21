"""ML feature fidelity — pins the collector against the SHAPE THE FETCHER ACTUALLY EMITS.

Background: pcr_oi was pinned to exactly 1.0 on every row ever collected because
MLCollector._compute_totals_from_chain read nested ``{"ce": {"oi": ...}}`` keys while
OIFetcher.fetch_chain emits FLAT ``ce_oi`` / ``pe_oi`` rows. The pre-existing collector
test mocked the chain in the nested shape, so it asserted the bug was correct.

These tests build chain rows from the fetcher's real key set, so a future shape change
on either side fails here instead of silently zeroing the feature.
"""

import unittest

from ml_signal.collector import MLCollector
from ml_signal.features import compute_iv_features, compute_oi_features


# The exact keys OIFetcher.fetch_chain appends per strike. Kept as a literal so a
# rename on the fetcher side breaks this test loudly.
FETCHER_CHAIN_KEYS = {
    "strike",
    "ce_oi", "ce_oi_prev", "ce_oi_change_pct", "ce_ltp", "ce_iv", "ce_delta",
    "pe_oi", "pe_oi_prev", "pe_oi_change_pct", "pe_ltp", "pe_iv", "pe_delta",
}


def _chain_row(strike, ce_oi, pe_oi):
    """A chain row in the fetcher's real flat shape."""
    return {
        "strike": strike,
        "ce_oi": ce_oi, "ce_oi_prev": ce_oi, "ce_oi_change_pct": 0.0,
        "ce_ltp": 100.0, "ce_iv": 15.0, "ce_delta": 0.5,
        "pe_oi": pe_oi, "pe_oi_prev": pe_oi, "pe_oi_change_pct": 0.0,
        "pe_ltp": 100.0, "pe_iv": 16.0, "pe_delta": -0.5,
    }


class TestChainTotalsMatchFetcherShape(unittest.TestCase):

    def test_fixture_matches_the_fetchers_key_set(self):
        """If this fails, the fetcher changed shape and the fixture is now a lie."""
        self.assertEqual(set(_chain_row(24000, 1, 1).keys()), FETCHER_CHAIN_KEYS)

    def test_totals_are_summed_from_flat_keys(self):
        collector = MLCollector.__new__(MLCollector)  # no Supabase client needed
        chain = [
            _chain_row(24000, ce_oi=1_000_000, pe_oi=3_000_000),
            _chain_row(24100, ce_oi=2_000_000, pe_oi=1_000_000),
        ]

        totals = collector._compute_totals_from_chain(chain)

        self.assertEqual(totals["total_ce_oi"], 3_000_000)
        self.assertEqual(totals["total_pe_oi"], 4_000_000)
        self.assertEqual(totals["all_ce_oi"], [1_000_000, 2_000_000])
        self.assertEqual(totals["all_pe_oi"], [3_000_000, 1_000_000])

    def test_pcr_is_the_real_ratio_not_the_divide_guard(self):
        """Regression: real NIFTY PCR sits near 0.73, never exactly 1.0."""
        collector = MLCollector.__new__(MLCollector)
        chain = [_chain_row(24000, ce_oi=366_983_500, pe_oi=268_724_755)]

        totals = collector._compute_totals_from_chain(chain)
        feats = compute_oi_features(
            atm_ce_oi=17_537_975, atm_pe_oi=26_970_970,
            total_ce_oi=totals["total_ce_oi"], total_pe_oi=totals["total_pe_oi"],
            ce_oi_change_pct=0.0, pe_oi_change_pct=0.0,
            all_ce_oi=totals["all_ce_oi"], all_pe_oi=totals["all_pe_oi"],
        )

        self.assertAlmostEqual(feats["pcr_oi"], 0.7323, places=3)
        self.assertGreater(feats["oi_concentration"], 0.0)

    def test_empty_chain_reports_missing_not_neutral(self):
        """No chain data must be distinguishable from a genuinely neutral PCR."""
        feats = compute_oi_features(
            atm_ce_oi=0, atm_pe_oi=0, total_ce_oi=0, total_pe_oi=0,
            ce_oi_change_pct=0.0, pe_oi_change_pct=0.0,
        )
        self.assertIsNone(feats["pcr_oi"])


class TestIVHistoryOrdering(unittest.TestCase):
    """iv_history must EXCLUDE the current bar — otherwise every diff is self-vs-self."""

    def test_iv_change_1_tracks_a_moving_iv(self):
        feats = compute_iv_features(
            current_iv=14.0, iv_ce=14.0, iv_pe=14.5,
            iv_history=[15.0, 14.5],  # prior bars only
        )
        self.assertAlmostEqual(feats["iv_change_1"], -0.5)

    def test_iv_change_5_spans_five_bars(self):
        feats = compute_iv_features(
            current_iv=10.0, iv_ce=10.0, iv_pe=10.0,
            iv_history=[15.0, 14.0, 13.0, 12.0, 11.0],
        )
        self.assertAlmostEqual(feats["iv_change_5"], -5.0)

    def test_iv_acceleration_includes_the_current_bar(self):
        # series 10, 11, 13 -> changes +1, +2 -> acceleration +1
        feats = compute_iv_features(
            current_iv=13.0, iv_ce=13.0, iv_pe=13.0,
            iv_history=[10.0, 11.0],
        )
        self.assertAlmostEqual(feats["iv_acceleration"], 1.0)

    def test_iv_percentile_can_reach_100(self):
        """With the current bar wrongly inside history, this capped at 95.0."""
        feats = compute_iv_features(
            current_iv=99.0, iv_ce=99.0, iv_pe=99.0,
            iv_history=[float(i) for i in range(20)],
        )
        self.assertEqual(feats["iv_percentile"], 100.0)


class TestHistoryContractHoldsForEveryCaller(unittest.TestCase):
    """Both feature producers must pass PRIOR-bar history.

    MLCollector.snapshot and LivePredictionLoop.run are independent callers of the
    same compute functions. Fixing one and not the other silently corrupts the other's
    features, so the ordering is asserted by source inspection for both.
    """

    def _append_lines_are_after_the_compute_call(self, source, compute_marker, appends):
        compute_at = source.index(compute_marker)
        for append in appends:
            self.assertIn(append, source, f"{append!r} moved or was renamed")
            self.assertGreater(
                source.index(append), compute_at,
                f"{append!r} must run AFTER {compute_marker!r} — history must hold prior bars only",
            )

    def test_collector_appends_after_computing(self):
        import inspect
        from ml_signal import collector
        self._append_lines_are_after_the_compute_call(
            inspect.getsource(collector.MLCollector.snapshot),
            "compute_iv_features(",
            ["self.volume_history.append(", "self.iv_history.append("],
        )

    def test_live_loop_appends_after_predicting(self):
        import inspect
        from ml_signal import live
        cls = next(
            obj for _, obj in vars(live).items()
            if inspect.isclass(obj) and hasattr(obj, "run") and obj.__module__ == live.__name__
        )
        self._append_lines_are_after_the_compute_call(
            inspect.getsource(cls.run),
            "predict_from_raw(",
            ["self.volume_history.append(", "self.iv_history.append("],
        )


if __name__ == "__main__":
    unittest.main()
