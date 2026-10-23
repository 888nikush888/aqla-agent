"""One financial writer per account, with reentrant bounded priority work packets."""

import asyncio
import heapq
import itertools
import time
from contextlib import asynccontextmanager

from aqla_hyperliquid_mcp.models import require_first


class AccountLock:
    def __init__(self):
        self.owner, self.depth = None, 0
        self.waiters, self.sequence = [], itertools.count()
        self.metrics = {}

    def observed_wait(self, priority, started):
        elapsed = max(0, (time.monotonic() - started) * 1000)
        row = self.metrics.setdefault(str(priority), {"acquisitions": 0, "total_wait_ms": 0.0, "max_wait_ms": 0.0})
        row.update(acquisitions=row["acquisitions"] + 1, total_wait_ms=row["total_wait_ms"] + elapsed, max_wait_ms=max(row["max_wait_ms"], elapsed))

    def locked(self):
        return self.owner is not None

    async def acquire(self, priority=10):
        task = asyncio.current_task()
        started = time.monotonic()
        if self.owner is task:
            self.depth += 1
            return True
        if self.owner is None and not self.waiters:
            self.owner, self.depth = task, 1
            self.observed_wait(priority, started)
            return True
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self.waiters, (priority, require_first(self.sequence), future, task))
        try:
            await future
            self.observed_wait(priority, started)
            return True
        except asyncio.CancelledError:
            future.cancel()
            if self.owner is task:
                self.release()
            raise

    def release(self):
        if self.owner is not asyncio.current_task():
            raise RuntimeError("Account lock can only be released by its owner")
        self.depth -= 1
        if self.depth:
            return
        self.owner = None
        while self.waiters:
            _, _, future, task = heapq.heappop(self.waiters)
            if not future.done():
                self.owner, self.depth = task, 1
                future.set_result(True)
                break

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        self.release()

    @asynccontextmanager
    async def work(self, priority):
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()
