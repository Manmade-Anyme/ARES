"""TASK-194 — make ml_collection trainable and make the OI-wall threshold answerable.

Three defects, all of which let ml_collection accumulate 9,102 rows that could
train nothing:

1. ``ml_collection.signal_id`` stored ``signal.signal_id`` — a RANDOM 4-digit
   display id (``models.py``: ``f"{random.randint(0, 9999):04d}"``). Meanwhile
   ``trade_analytics.signal_id`` stores ``signal.db_id`` (the real
   ``ares_signals.id``). Verified against prod: 0 of 119 rows overlapped, so the
   two tables were never joinable and the label columns had no key to write
   against. ``trade_id`` / ``trade_outcome`` / ``trade_pnl`` were 0-filled on
   every row ever collected.

2. ``compute_oi_features`` received the full per-strike OI distribution and used
   it only to compute a SUM, discarding the shape. That makes the proposed
   "wall = OI >= p85 of strikes with non-zero OI" rule untestable against
   history, and hides that ``oi_wall_min_oi = 4_000_000`` sits above the entire
   live chain for most of a weekly cycle.

3. ``compute_structure_features`` returned a literal ``100.0`` when no level was
   found — a "no data" marker indistinguishable from a real 100-point distance.
   57% of prod rows carry it. ``_numeric_only`` drops ``None``, so emitting
   ``None`` lands as NaN in the training matrix, which XGBoost handles natively.

These tests build chain rows in the fetcher's real flat shape, per the standing
lesson that mocking the boundary is how wrong-but-well-formed values ship green.
"""

import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from models import AresSignal, Direction, SetupType, OHLCVCandle
from ml_signal.collector import MLCollector
from ml_signal.features import compute_oi_features, compute_structure_features


def _chain_row(strike, ce_oi, pe_oi):
    """A chain row in OIFetcher.fetch_chain's real FLAT shape."""
    return {
        "strike": strike,
        "ce_oi": ce_oi, "ce_oi_prev": ce_oi, "ce_oi_change_pct": 0.0,
        "ce_ltp": 100.0, "ce_iv": 15.0, "ce_delta": 0.5,
        "pe_oi": pe_oi, "pe_oi_prev": pe_oi, "pe_oi_change_pct": 0.0,
        "pe_ltp": 100.0, "pe_iv": 16.0, "pe_delta": -0.5,
    }


class _Row:
    def __init__(self):
        self.iv, self.oi, self.oi_change_pct = 15.0, 500_000, 2.5
        self.gamma, self.theta, self.vega = 0.05, -0.8, 0.3


class _ATM:
    def __init__(self):
        self.ce, self.pe, self.strike = _Row(), _Row(), 24000.0


def _candle():
    return OHLCVCandle(
        timestamp=datetime(2026, 7, 31, 10, 32),
        open=24000.0, high=24010.0, low=23995.0, close=24002.0,
        volume=150_000, vwap=24001.0,
    )


def _signal(db_id):
    s = AresSignal(
        setup_type=SetupType.OI_WALL_REJECTION,
        direction=Direction.BEARISH,
        trigger_price=24002.0,
        entry_zone=(24000.0, 24004.0),
        stop_loss=24014.0,
        target_1=23978.0,
        target_2=23920.0,
        confidence="HIGH",
        reasons=["test"],
        timestamp=datetime(2026, 7, 31, 10, 32),
        strike_to_trade=24000,
        option_type="PE",
    )
    s.db_id = db_id
    return s


def _collector():
    with patch("ml_signal.collector.create_client", return_value=MagicMock()):
        return MLCollector("http://supabase.invalid", "key")


def _snapshot_record(collector, signal=None, chain=None):
    """Run snapshot and return the record it tried to insert."""
    captured = {}
    collector._insert = lambda rec: captured.update(rec)
    collector.snapshot(
        candle=_candle(), atm=_ATM(),
        full_chain=chain if chain is not None else [_chain_row(24000, 100, 100)],
        levels=[], spot=24002.0, signal=signal,
        pdh=24100.0, pdl=23900.0, is_expiry=False, dte=3,
        timestamp=datetime(2026, 7, 31, 10, 32),
    )
    return captured


class TestOIDistributionCaptured(unittest.TestCase):
    """Defect 2 — the chain's shape must survive, not just its sum."""

    def test_max_and_p85_are_exposed(self):
        # 10 strikes, CE OI 100..1000. p85 of a 10-point set is a real quantile,
        # and max is unambiguous.
        chain = [_chain_row(23500 + i * 50, (i + 1) * 100, (i + 1) * 10)
                 for i in range(10)]
        ce = [r["ce_oi"] for r in chain]
        pe = [r["pe_oi"] for r in chain]

        feats = compute_oi_features(
            atm_ce_oi=500, atm_pe_oi=500,
            total_ce_oi=sum(ce), total_pe_oi=sum(pe),
            ce_oi_change_pct=0.0, pe_oi_change_pct=0.0,
            all_ce_oi=ce, all_pe_oi=pe,
        )

        self.assertEqual(feats["max_ce_oi"], 1000)
        self.assertEqual(feats["max_pe_oi"], 100)
        # The wall rule the feasibility doc proposes needs this specific number.
        self.assertIn("p85_ce_oi", feats)
        self.assertIn("p85_pe_oi", feats)
        self.assertGreater(feats["p85_ce_oi"], feats["max_ce_oi"] * 0.5)
        self.assertLessEqual(feats["p85_ce_oi"], feats["max_ce_oi"])

    def test_zero_oi_strikes_excluded_from_percentile(self):
        """'p85 of strikes with NON-ZERO OI' — dead strikes must not drag it down."""
        live = [1000, 2000, 3000, 4000]
        padded = live + [0] * 40

        dense = compute_oi_features(
            atm_ce_oi=0, atm_pe_oi=0, total_ce_oi=sum(live), total_pe_oi=sum(live),
            ce_oi_change_pct=0.0, pe_oi_change_pct=0.0,
            all_ce_oi=live, all_pe_oi=live,
        )
        sparse = compute_oi_features(
            atm_ce_oi=0, atm_pe_oi=0, total_ce_oi=sum(live), total_pe_oi=sum(live),
            ce_oi_change_pct=0.0, pe_oi_change_pct=0.0,
            all_ce_oi=padded, all_pe_oi=padded,
        )
        self.assertEqual(dense["p85_ce_oi"], sparse["p85_ce_oi"])
        self.assertEqual(sparse["strikes_with_ce_oi"], 4)

    def test_empty_chain_does_not_raise_or_fabricate(self):
        feats = compute_oi_features(
            atm_ce_oi=0, atm_pe_oi=0, total_ce_oi=0, total_pe_oi=0,
            ce_oi_change_pct=0.0, pe_oi_change_pct=0.0,
            all_ce_oi=[], all_pe_oi=[],
        )
        # None, not 0 — an absent chain must stay distinguishable from a real zero.
        self.assertIsNone(feats["max_ce_oi"])
        self.assertIsNone(feats["p85_ce_oi"])
        self.assertEqual(feats["strikes_with_ce_oi"], 0)

    def test_distribution_survives_the_collector_end_to_end(self):
        chain = [_chain_row(23800 + i * 50, (i + 1) * 1000, 500) for i in range(8)]
        rec = _snapshot_record(_collector(), chain=chain)
        oi = json.loads(rec["oi_features"])
        self.assertEqual(oi["max_ce_oi"], 8000)
        self.assertEqual(oi["strikes_with_ce_oi"], 8)


class TestSignalIdIsJoinable(unittest.TestCase):
    """Defect 1 — the key that makes labels writable at all."""

    def test_snapshot_stores_db_id_not_the_random_display_id(self):
        sig = _signal(db_id=271)
        rec = _snapshot_record(_collector(), signal=sig)
        # The bug: str(signal.signal_id) — a random 4-digit string.
        self.assertNotEqual(str(rec["signal_id"]), str(sig.signal_id))
        self.assertEqual(str(rec["signal_id"]), "271")

    def test_missing_db_id_writes_null_not_a_fake_key(self):
        """log_signal failed -> no row to join to. NULL is honest; a random id is not."""
        rec = _snapshot_record(_collector(), signal=_signal(db_id=None))
        self.assertIsNone(rec["signal_id"])

    def test_no_signal_still_snapshots_with_null_key(self):
        rec = _snapshot_record(_collector(), signal=None)
        self.assertIsNone(rec["signal_id"])
        self.assertFalse(rec["signal_generated"])


class TestStructureSentinel(unittest.TestCase):
    """Defect 3 — 'no level found' must not masquerade as a 100-point distance."""

    def test_absent_levels_emit_none_not_100(self):
        f = compute_structure_features(spot=24000.0, levels=[], full_chain=[],
                                       pdh=None, pdl=None)
        for k in ("dist_to_nearest_resistance", "dist_to_nearest_support",
                  "dist_to_pdh", "dist_to_pdl"):
            self.assertIsNone(f[k], f"{k} must be None when unknown, not a sentinel")

    def test_real_distances_still_computed(self):
        f = compute_structure_features(spot=24000.0, levels=[24050.0, 23950.0],
                                       full_chain=[], pdh=24100.0, pdl=23900.0)
        self.assertAlmostEqual(f["dist_to_nearest_resistance"], 50.0)
        self.assertAlmostEqual(f["dist_to_nearest_support"], 50.0)
        self.assertAlmostEqual(f["dist_to_pdh"], 100.0)
        self.assertAlmostEqual(f["dist_to_pdl"], 100.0)

    def test_a_genuine_100pt_distance_is_not_confused_with_missing(self):
        f = compute_structure_features(spot=24000.0, levels=[24100.0],
                                       full_chain=[], pdh=None, pdl=None)
        self.assertAlmostEqual(f["dist_to_nearest_resistance"], 100.0)
        self.assertIsNone(f["dist_to_nearest_support"])


class TestLabelBackfillOnTradeClose(unittest.TestCase):
    """Defect 1, second half — closing a trade must write the label columns."""

    def _logger(self):
        import storage
        with patch.object(storage, "create_client", return_value=MagicMock()):
            return storage.AnalyticsLogger()

    def test_log_exit_writes_outcome_to_ml_collection(self):
        lg = self._logger()
        sb = lg.supabase

        trade_row = {"entry_price": 24002.0, "direction": "BEARISH", "signal_id": 271}
        sb.table.return_value.select.return_value.eq.return_value.execute.return_value = \
            MagicMock(data=[trade_row])

        lg.log_exit("trade-uuid-1", exit_price=23960.0, final_state="T2_HIT")

        updates = [c for c in sb.table.return_value.update.call_args_list]
        self.assertTrue(updates, "no update issued at all")
        payloads = [c.args[0] if c.args else c.kwargs.get("json", {}) for c in updates]

        ml = [p for p in payloads if "trade_outcome" in p]
        self.assertTrue(ml, "trade close never wrote the ml_collection label columns")
        label = ml[0]
        self.assertEqual(label["trade_outcome"], "T2_HIT")
        self.assertEqual(label["trade_id"], "trade-uuid-1")
        self.assertAlmostEqual(float(label["trade_pnl"]), 42.0)  # bearish 24002 -> 23960

    def test_trade_without_signal_id_is_skipped_not_crashed(self):
        lg = self._logger()
        sb = lg.supabase
        sb.table.return_value.select.return_value.eq.return_value.execute.return_value = \
            MagicMock(data=[{"entry_price": 24002.0, "direction": "BULLISH",
                             "signal_id": None}])
        lg.log_exit("trade-uuid-2", exit_price=24050.0, final_state="T1_HIT")
        payloads = [c.args[0] for c in sb.table.return_value.update.call_args_list if c.args]
        self.assertFalse([p for p in payloads if "trade_outcome" in p],
                         "orphan trade must not write a label against a null key")


if __name__ == "__main__":
    unittest.main()
