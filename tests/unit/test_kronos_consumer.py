import unittest
import pandas as pd

from ml_signal.kronos_consumer import barrier_hit_fraction, format_kronos_alert


def _paths(*close_series):
    return [pd.DataFrame({"close": cs}) for cs in close_series]


class TestBarrierHitFraction(unittest.TestCase):

    def test_bullish_clean_target_hit(self):
        paths = _paths([100, 105, 110], [100, 102, 104])
        result = barrier_hit_fraction(paths, entry=100, target=108, stop=95, bullish=True)
        self.assertAlmostEqual(result, 0.5)

    def test_bullish_stop_hit_first_excludes_target(self):
        # touches stop (95) before it would later cross target (110) — must not count as a hit
        paths = _paths([100, 94, 111])
        result = barrier_hit_fraction(paths, entry=100, target=110, stop=95, bullish=True)
        self.assertEqual(result, 0.0)

    def test_bearish_direction_flips_comparisons(self):
        paths = _paths([100, 95, 90], [100, 103, 106])
        result = barrier_hit_fraction(paths, entry=100, target=92, stop=105, bullish=False)
        self.assertAlmostEqual(result, 0.5)

    def test_neither_barrier_touched_counts_as_miss(self):
        paths = _paths([100, 101, 99])
        result = barrier_hit_fraction(paths, entry=100, target=110, stop=90, bullish=True)
        self.assertEqual(result, 0.0)

    def test_empty_paths_returns_nan(self):
        result = barrier_hit_fraction([], entry=100, target=110, stop=90, bullish=True)
        self.assertTrue(pd.isna(result))


class TestFormatKronosAlert(unittest.TestCase):

    def test_includes_signal_id_and_probability(self):
        msg = format_kronos_alert(signal_id="42", setup_type="EXHAUSTION_REVERSAL",
                                   probability=0.62, model_name="NeoQuasar/Kronos-mini")
        self.assertIn("#42", msg)
        self.assertIn("EXHAUSTION_REVERSAL", msg)
        self.assertIn("62.0%", msg)


if __name__ == "__main__":
    unittest.main()
