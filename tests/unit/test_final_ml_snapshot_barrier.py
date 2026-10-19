"""A final non-signal snapshot must await the actual database insert."""
import asyncio
from datetime import datetime
from threading import Event
from types import SimpleNamespace

import pytest

import ml_signal.collector as collector_module
from models import ATMStrikes, OHLCVCandle, OptionRow


class DelayedDatabase:
    """Gate the external execute call while using the real collector pipeline."""
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.release = Event()
        self.finished = Event()
        self.records = []
        self.failure = None

    def table(self, name):
        assert name == "ml_collection"
        database = self

        class Insert:
            def insert(self, record):
                self.record = record
                return self

            def execute(self):
                database.loop.call_soon_threadsafe(database.started.set)
                try:
                    if not database.release.wait(2):
                        raise RuntimeError("Test did not release the database insert")
                    if database.failure:
                        raise database.failure
                    database.records.append(self.record)
                    return SimpleNamespace(data=[self.record])
                finally:
                    database.finished.set()

        return Insert()


@pytest.fixture
def sample():
    return dict(
        candle=OHLCVCandle(datetime(2026, 10, 19, 15, 29), 24000, 24020, 23980, 24005, 100, 24000),
        atm=ATMStrikes(24005, OptionRow(24000, "CE", iv=15, oi=100),
                       OptionRow(24000, "PE", iv=16, oi=200)),
        full_chain=[{"strike": 24000, "ce_oi": 100, "pe_oi": 200}],
        levels=[], spot=24005,
    )


def make_collector(monkeypatch, database):
    monkeypatch.setattr(collector_module, "create_client", lambda *_: database)
    monkeypatch.setattr(collector_module.settings, "signal_schema_mode", "bridge")
    return collector_module.MLCollector("https://example.invalid", "test")


@pytest.mark.asyncio
async def test_final_no_signal_snapshot_waits_for_real_insert(monkeypatch, sample):
    database = DelayedDatabase()
    collector = make_collector(monkeypatch, database)
    task = asyncio.create_task(collector.snapshot(**sample, wait_for_completion=True))
    try:
        await asyncio.wait_for(database.started.wait(), 1)
        assert not task.done()
        assert database.records == []
        database.release.set()
        await task
        assert len(database.records) == 1
        assert database.records[0]["signal_generated"] is False
        assert database.records[0]["spot"] == 24005
    finally:
        database.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ordinary_no_signal_snapshot_remains_background(monkeypatch, sample):
    database = DelayedDatabase()
    collector = make_collector(monkeypatch, database)
    try:
        await asyncio.wait_for(collector.snapshot(**sample), 1)
        await asyncio.wait_for(database.started.wait(), 1)
        assert database.records == []
        database.release.set()
        assert await asyncio.to_thread(database.finished.wait, 1)
        assert len(database.records) == 1
    finally:
        database.release.set()


@pytest.mark.asyncio
async def test_final_no_signal_insert_failure_reaches_caller(monkeypatch, sample):
    database = DelayedDatabase()
    database.release.set()
    database.failure = RuntimeError("Database insert unavailable")
    collector = make_collector(monkeypatch, database)
    with pytest.raises(RuntimeError, match="Database insert unavailable"):
        await collector.snapshot(**sample, wait_for_completion=True)
    assert database.records == []
