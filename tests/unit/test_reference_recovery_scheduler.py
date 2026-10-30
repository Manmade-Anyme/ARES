import asyncio
from types import SimpleNamespace

import pytest

import main
from position_manager import PositionManager


@pytest.mark.asyncio
async def test_recovery_is_background_nonoverlapping_and_restarts_for_new_leg(monkeypatch):
    import execution.reference_recovery as recovery

    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def worker(manager, fetcher, analytics):
        calls.append(1)
        started.set()
        await release.wait()

    monkeypatch.setattr(recovery, "run_reference_recovery", worker)
    manager = PositionManager.__new__(PositionManager)
    manager.analytics = SimpleNamespace(_option_backend_configured=True)
    manager.active_trades = [{"id": "t", "market_context": {"option_trailing": {
        "accounting_formula_version": "one_lot_reference_v1", "accounting": {"legs": {}}
    }}}]
    fetcher = object()
    main._schedule_reference_recovery(manager, fetcher)
    await started.wait()
    main._schedule_reference_recovery(manager, fetcher)
    assert len(calls) == 1
    release.set()
    await manager._reference_recovery_task
    main._schedule_reference_recovery(manager, fetcher)
    assert len(calls) == 1
    manager.active_trades[0]["market_context"]["option_trailing"]["accounting"]["legs"]["T1"] = {}
    main._schedule_reference_recovery(manager, fetcher)
    await manager._reference_recovery_task
    assert len(calls) == 2
