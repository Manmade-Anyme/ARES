import asyncio
import threading
from unittest.mock import patch

import pytest

from fetchers.price_fetcher import PriceFetcher, ReferencePriceDeferred


@pytest.mark.asyncio
async def test_protection_runs_while_background_request_is_stalled():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    entered = threading.Event()
    release = threading.Event()
    def background():
        entered.set()
        release.wait(timeout=3)
        return "background"
    task = asyncio.create_task(fetcher._reference_request(background))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await asyncio.wait_for(fetcher._protective_request(lambda: "protect"), 0.5) == "protect"
        assert not task.done()
    finally:
        release.set()
        assert await task == "background"


@pytest.mark.asyncio
async def test_background_defers_while_protection_active_and_during_cooldown():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    entered = threading.Event()
    release = threading.Event()
    def protected():
        entered.set()
        release.wait(timeout=3)
        return "protect"
    with patch("fetchers.price_fetcher.monotonic_time.monotonic", return_value=100):
        task = asyncio.create_task(fetcher._protective_request(protected))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            with pytest.raises(ReferencePriceDeferred):
                await fetcher._reference_request(lambda: pytest.fail("must not call Dhan"))
        finally:
            release.set()
            assert await task == "protect"
        with pytest.raises(ReferencePriceDeferred):
            await fetcher._reference_request(lambda: pytest.fail("must respect cooldown"))
    with patch("fetchers.price_fetcher.monotonic_time.monotonic", return_value=102):
        assert await fetcher._reference_request(lambda: "recovered") == "recovered"


@pytest.mark.asyncio
async def test_cancelled_protective_wait_does_not_release_running_request():
    fetcher = PriceFetcher.__new__(PriceFetcher)
    entered = threading.Event()
    release = threading.Event()
    def protected():
        entered.set()
        release.wait(timeout=3)
    task = asyncio.create_task(fetcher._protective_request(protected))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ReferencePriceDeferred):
            await fetcher._reference_request(lambda: pytest.fail("still protected"))
    finally:
        release.set()
        for _ in range(100):
            if not fetcher._protective_active:
                break
            await asyncio.sleep(0.001)
        assert fetcher._protective_active == 0
