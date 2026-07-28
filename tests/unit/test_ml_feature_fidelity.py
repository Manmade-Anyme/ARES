"""ML feature fidelity — pins the collector against the SHAPE THE FETCHER ACTUALLY EMITS.

Background: pcr_oi was pinned to exactly 1.0 on every row ever collected because
MLCollector._compute_totals_from_chain read nested ``{"ce": {"oi": ...}}`` keys while
OIFetcher.fetch_chain emits FLAT ``ce_oi`` / ``pe_oi`` rows. The pre-existing collector
test mocked the chain in the nested shape, so it asserted the bug was correct.

These tests build chain rows from the fetcher's real key set, so a future shape change
on either side fails here instead of silently zeroing the feature.
"""

import json
import unittest
from collections import deque
from datetime import datetime, timedelta

from models import AresSignal, Direction, SetupType
from ml_signal.collector import MLCollector
from ml_signal.config import DEFAULT_CONFIG
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


def _candle():
    """A real-shaped candle. Plain object, not a mock — attributes must stringify sanely."""
    from models import OHLCVCandle
    return OHLCVCandle(
        timestamp=datetime(2026, 7, 28, 10, 32),
        open=24000.0, high=24010.0, low=23995.0, close=24002.0,
        volume=150_000, vwap=24001.0,
    )


class _Row:
    def __init__(self):
        self.iv, self.oi, self.oi_change_pct = 15.0, 500_000, 2.5
        self.gamma, self.theta, self.vega = 0.05, -0.8, 0.3
        self.oi_prev = 490_000


class _ATM:
    def __init__(self):
        self.ce, self.pe = _Row(), _Row()


def _atm():
    return _ATM()


def _signal(setup_type, direction=Direction.BULLISH):
    """A real AresSignal carrying real enum members."""
    return AresSignal(
        setup_type=setup_type,
        direction=direction,
        trigger_price=24000.0,
        entry_zone=(23995.0, 24005.0),
        stop_loss=23988.0,
        target_1=24020.0,
        target_2=24040.0,
        confidence="HIGH",
        reasons=["test"],
        timestamp=datetime(2026, 7, 28, 10, 32),
        strike_to_trade=24000,
        option_type="CE" if direction is Direction.BULLISH else "PE",
    )


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


class TestSignalColumnsCarryEnumValues(unittest.TestCase):
    """Regression: every ``detector_scores`` row ever collected was all-zero.

    ``str(SetupType.OI_WALL_REJECTION)`` is ``'SetupType.OI_WALL_REJECTION'``, which
    never equals the bare ``'OI_WALL_REJECTION'`` the one-hot compared against — so the
    column was well-formed and constantly wrong. These tests pass a REAL AresSignal
    (never a MagicMock, whose attributes stringify to something arbitrary) so the
    assertion is against the shape the engine actually emits.
    """

    def _snapshot_record(self, signal, **kwargs):
        """Run snapshot() with the Supabase client stubbed and return the record."""
        from unittest.mock import patch
        from ml_signal.collector import MLCollector

        collector = MLCollector.__new__(MLCollector)
        collector.config = DEFAULT_CONFIG
        collector.volume_history = deque(maxlen=20)
        collector.iv_history = deque(maxlen=20)
        collector._total_snapshots = 0
        collector._signals_recorded = 0

        captured = {}
        with patch.object(MLCollector, "_insert", lambda self, record: captured.update(record)):
            collector.snapshot(
                candle=_candle(),
                atm=_atm(),
                full_chain=[_chain_row(24000, ce_oi=1_000_000, pe_oi=1_000_000)],
                levels=[],
                spot=24000.0,
                signal=signal,
                **kwargs,
            )
        return captured

    def test_each_setup_type_sets_exactly_its_own_flag(self):
        for setup in SetupType:
            with self.subTest(setup=setup):
                record = self._snapshot_record(_signal(setup))
                scores = json.loads(record["detector_scores"])

                self.assertEqual(
                    scores.get(setup.value.lower()), 1,
                    f"{setup.value} must set its own flag to 1, got {scores}",
                )
                self.assertEqual(
                    sum(scores.values()), 1,
                    f"exactly one flag may be set for {setup.value}, got {scores}",
                )

    def test_every_setup_type_has_a_flag(self):
        """TREND_CONTINUATION had no key at all, so it scored as all-zeros."""
        scores = json.loads(
            self._snapshot_record(_signal(SetupType.FAILED_BREAKOUT))["detector_scores"]
        )
        self.assertEqual(
            set(scores), {s.value.lower() for s in SetupType},
            "detector_scores must carry one key per SetupType member",
        )

    def test_no_signal_leaves_every_flag_zero(self):
        scores = json.loads(self._snapshot_record(None)["detector_scores"])
        self.assertEqual(set(scores.values()), {0})

    def test_setup_and_direction_store_bare_enum_values(self):
        record = self._snapshot_record(
            _signal(SetupType.EXHAUSTION_REVERSAL, Direction.BEARISH)
        )
        self.assertEqual(record["signal_setup_type"], "EXHAUSTION_REVERSAL")
        self.assertEqual(record["signal_direction"], "BEARISH")

    def test_dte_reaches_meta_features(self):
        record = self._snapshot_record(None, is_expiry=True, dte=0)
        meta = json.loads(record["meta_features"])

        self.assertEqual(meta["is_expiry_day"], 1)
        self.assertEqual(
            meta["dte"], 0.0,
            "an expiry-day row must record dte=0, not the 7.0 fallback",
        )


class TestDaysToExpiry(unittest.TestCase):
    """dte was pinned at the 7.0 fallback on every row because main.py passed None."""

    def test_expiry_today_is_zero(self):
        from detectors.expiry_detector import days_to_expiry, _today_ist
        self.assertEqual(days_to_expiry(_today_ist().strftime("%Y-%m-%d")), 0)

    def test_future_expiry_counts_calendar_days(self):
        from detectors.expiry_detector import days_to_expiry, _today_ist
        future = _today_ist() + timedelta(days=7)
        self.assertEqual(days_to_expiry(future.strftime("%Y-%m-%d")), 7)

    def test_unparseable_input_returns_none_not_a_wrong_number(self):
        from detectors.expiry_detector import days_to_expiry
        for bad in ["", None, "not-a-date", "28-07-2026"]:
            with self.subTest(bad=bad):
                self.assertIsNone(days_to_expiry(bad))

    def test_main_does_not_hardcode_dte(self):
        """Regression: the snapshot call passed a literal dte=None for months."""
        import inspect
        import main
        source = inspect.getsource(main)
        self.assertNotIn(
            "dte=None", source,
            "main.py must pass a computed dte, not the None that forces the 7.0 fallback",
        )

    def test_live_serving_path_does_not_hardcode_dte(self):
        """Collection and serving must agree, or the model trains on a real dte and
        is served the 7.0 fallback (training-serving skew)."""
        import inspect
        from ml_signal import live
        source = inspect.getsource(live)
        self.assertNotIn(
            "dte=None", source,
            "ml_signal/live.py must pass the same computed dte that MLCollector records",
        )


if __name__ == "__main__":
    unittest.main()
