import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from storage import AnalyticsLogger


class _Query:
    def __init__(self, database, table):
        self.database = database
        self.table = table
        self.filters = []
        self.columns = None
        self.operation = "select"
        self.payload = None

    def select(self, columns, *additional_columns, **_kwargs):
        self.columns = []
        for value in (columns, *additional_columns):
            self.columns.extend(value.split(","))
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def is_(self, column, value):
        self.filters.append((column, None if value == "null" else value))
        return self

    def execute(self):
        if self.operation == "insert":
            self.database.insert_gate.wait()
            self.database.rows[self.table].append(dict(self.payload))
            return SimpleNamespace(data=[dict(self.payload)])

        matching = [
            row for row in self.database.rows[self.table]
            if all(row.get(column) == value for column, value in self.filters)
        ]
        if self.operation == "update":
            for row in matching:
                row.update(self.payload)
            return SimpleNamespace(data=matching)

        if self.columns is not None:
            matching = [
                {column: row.get(column) for column in self.columns}
                for row in matching
            ]
        return SimpleNamespace(data=matching)


class _Database:
    def __init__(self, rows=None):
        self.rows = rows or {"trade_analytics": [], "ml_collection": []}
        self.insert_gate = threading.Event()
        self.insert_gate.set()

    def table(self, name):
        return _Query(self, name)


def _signal():
    return SimpleNamespace(
        signal_id="0042",
        db_id=42,
        setup_type=SimpleNamespace(value="FAILED_BREAKOUT"),
        direction=SimpleNamespace(value="BULLISH"),
        timestamp="2026-09-13T04:00:00+00:00",
        reasons=[],
        trigger_price=24000.0,
        confidence="HIGH",
        suggested_lots=None,
        oi_wall_context=None,
    )


class TestTask151RuntimeRegressions(unittest.TestCase):
    def test_log_exit_updates_only_the_time_correlated_ml_row(self):
        database = _Database({
            "trade_analytics": [{
                "id": "trade-1", "entry_price": 24000.0, "direction": "BULLISH",
                "signal_id": "0042", "setup_type": "FAILED_BREAKOUT",
                "entry_timestamp": "2026-09-13T04:00:00+00:00",
            }],
            "ml_collection": [
                {
                    "id": 10, "signal_id": "0042", "signal_setup_type": "FAILED_BREAKOUT",
                    "timestamp": "2026-09-13T04:00:01+00:00", "trade_id": None,
                },
                {
                    "id": 11, "signal_id": "0042", "signal_setup_type": "FAILED_BREAKOUT",
                    "timestamp": "2026-09-13T05:00:01+00:00", "trade_id": None,
                },
            ],
        })
        with patch("storage.create_client", return_value=database), patch(
            "storage.asyncio.get_running_loop", side_effect=RuntimeError
        ):
            logger = AnalyticsLogger()
            logger.log_exit("trade-1", 24050.0, "T1_HIT")

        self.assertEqual(database.rows["ml_collection"][0]["trade_id"], "trade-1")
        self.assertIsNone(database.rows["ml_collection"][1]["trade_id"])


class TestTask151EntryExitOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_exit_waits_for_its_slow_entry_insert(self):
        database = _Database()
        database.insert_gate.clear()
        with patch("storage.create_client", return_value=database):
            logger = AnalyticsLogger()
            logger.log_entry("trade-race", _signal(), 24000.0)
            logger.log_exit("trade-race", 24050.0, "T1_HIT")

            await asyncio.sleep(0.4)
            database.insert_gate.set()
            for _ in range(20):
                await asyncio.sleep(0.05)
                rows = database.rows["trade_analytics"]
                if rows and rows[0].get("result_state") == "T1_HIT":
                    break

        self.assertEqual(database.rows["trade_analytics"][0]["result_state"], "T1_HIT")


if __name__ == "__main__":
    unittest.main()
