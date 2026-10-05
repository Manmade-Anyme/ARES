"""Replay PostgreSQL timestamp precision through the public Jev signal path.

Uses only stdlib test tools so the same regression can run on the Docker
Python 3.10 runtime with database and TypeSafe transports replaced locally.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from system_one import consumer


class TimestampPrecisionTests(unittest.TestCase):
    def process(self, started, deadline):
        signal = {
            "signal_uuid": "signal-test", "direction": "BULLISH",
            "spot_at_signal": 100, "target_1": 110,
            "target_2": 120, "stop_loss": 90,
        }
        job = {
            "owner_token": "owner-test", "invocation_token": "invoke-test",
            "invocation_started_at": started,
            "invocation_dispatch_deadline_at": deadline,
        }
        predictions, requests = [], []
        database = MagicMock()
        database.table().select().eq().limit().execute.return_value.data = []

        def rpc(name, params):
            if name == "read_jev_signal":
                data = [signal]
            elif name in ("claim_llm_prediction_job", "begin_llm_invocation"):
                data = [job]
            elif name == "complete_llm_prediction_job":
                predictions.append(json.loads(json.dumps(params["p_prediction"])))
                data = [{"id": 1}]
            else:
                data = []
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))

        database.rpc.side_effect = rpc
        barrier = SimpleNamespace(choice="t1_first", confidence=.6,
                                  probabilities={"t1_first": .6, "sl_first": .4})
        target = SimpleNamespace(choice="t2_hits", confidence=.5,
                                 probabilities={"t2_hits": .5})
        regime = SimpleNamespace(choice="range_choppy", confidence=.7,
                                 probabilities={"range_choppy": .7})
        score = SimpleNamespace(score=2, confidence=.7, legend=list(range(5)), probabilities={2: 1})
        response = SimpleNamespace(
            model="jev-test", request_id="request-test",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
            choices={"first_barrier": barrier, "t2_given_t1": target,
                     "market_regime": regime},
            scores={name: score for name in ("price_action_strength",
                    "structural_clarity", "confluence_rating")},
            nouls={"is_trap": SimpleNamespace(noul=.2)},
        )

        def system_one(**kwargs):
            requests.append(kwargs)
            return response

        with patch("system_one.jev.TypeSafeClient") as client, \
                patch.object(consumer.time, "monotonic", return_value=100):
            client.return_value.__enter__.return_value.system_one.side_effect = system_one
            status = consumer.process_signal(database, {
                "signal_uuid": "signal-test", "snapshot_uuid": "snapshot-test",
                "timestamp": "2026-10-05T08:38:49+00:00", "spot": 100,
            })
        return status, predictions, requests

    def test_observed_four_and_five_digit_timestamps_archive_predictions(self):
        for started, deadline, budget in (
            ("2026-10-05T08:39:20.26328+00:00",
             "2026-10-05T08:39:22.26328+00:00", 2),
            ("2026-10-05T08:27:46.4949+00:00",
             "2026-10-05T08:27:48.4949+00:00", 2),
            ("2026-10-05T08:39:20.26328+00:00",
             "2026-10-05T08:39:21.26329+00:00", 1.00001),
        ):
            with self.subTest(started=started, deadline=deadline):
                status, predictions, requests = self.process(started, deadline)
                self.assertEqual(status, "COMPLETED")
                self.assertEqual(len(predictions), 1)
                self.assertEqual(predictions[0]["invocation_started_at"], started)
                self.assertAlmostEqual(requests[0]["timeout"], budget)

    def test_all_postgres_fractional_precisions_and_offsets(self):
        for precision in range(7):
            fraction = "." + "123456"[:precision] if precision else ""
            for offset in ("Z", "+00:00", "+05:30", "-04:00"):
                with self.subTest(precision=precision, offset=offset):
                    status, predictions, requests = self.process(
                        "2026-10-05T08:39:20" + fraction + offset,
                        "2026-10-05T08:39:22" + fraction + offset,
                    )
                    self.assertEqual(status, "COMPLETED")
                    self.assertEqual(len(predictions), 1)
                    self.assertAlmostEqual(requests[0]["timeout"], 2)

    def test_invalid_or_elapsed_window_cannot_invoke_or_archive(self):
        for started, deadline in (
            ("invalid", "2026-10-05T08:39:22.26328Z"),
            ("2026-10-05T08:39:20.26328Z", "invalid"),
            ("2026-10-05T08:39:20.26328Z", "2026-10-05T08:39:20.26328Z"),
            ("2026-10-05T08:39:20.26328Z", "2026-10-05T08:39:19.26328Z"),
        ):
            with self.subTest(started=started, deadline=deadline):
                self.assertEqual(self.process(started, deadline), ("UNKNOWN", [], []))


if __name__ == "__main__":
    unittest.main()
