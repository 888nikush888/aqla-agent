"""Context-local limits counted at HTTPX's actual outbound read boundary."""
from __future__ import annotations

import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

import httpx

from aqla_hyperliquid_mcp.models import AqlaError

BudgetCode = Literal["tick_request_limit", "tick_time_limit", "job_request_limit", "job_time_limit"]


class ReadBudgetExceeded(AqlaError):
    def __init__(self, code: BudgetCode):
        self.code = code
        super().__init__("Reporting read budget exhausted: " + code)


@dataclass
class ReadBudget:
    scope: Literal["tick", "job"]
    request_limit: int
    deadline: float
    clock: Callable[[], float] = time.monotonic
    requests: int = 0

    def check_time(self) -> None:
        if self.clock() >= self.deadline:
            raise ReadBudgetExceeded("tick_time_limit" if self.scope == "tick" else "job_time_limit")

    def check(self) -> None:
        self.check_time()
        if self.requests >= self.request_limit:
            raise ReadBudgetExceeded("tick_request_limit" if self.scope == "tick" else "job_request_limit")


@dataclass(frozen=True)
class RequestBudgets:
    limits: tuple[ReadBudget, ...]

    def spend(self) -> None:
        for limit in self.limits:
            limit.check()
        for limit in self.limits:
            limit.requests += 1


@dataclass(frozen=True)
class ReadBoundary:
    client: httpx.AsyncClient
    method: str
    path: str
    budgets: RequestBudgets


_limits: ContextVar[tuple[ReadBudget, ...]] = ContextVar("aqla_reporting_read_limits", default=())
_boundary: ContextVar[ReadBoundary | None] = ContextVar("aqla_observation_boundary", default=None)
_hooks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_EXTENSION = "aqla_reporting_read_budgets"


@contextmanager
def read_limits(*limits: ReadBudget) -> Iterator[None]:
    token = _limits.set(limits)
    try:
        yield
    finally:
        _limits.reset(token)


def _install_hook(client: httpx.AsyncClient) -> None:
    if client in _hooks:
        return
    reference = weakref.ref(client)
    async def count(request: httpx.Request) -> None:
        budgets = request.extensions.get(_EXTENSION)
        boundary = _boundary.get()
        if budgets is None and boundary is not None and reference() is boundary.client and request.method == boundary.method and request.url.path == boundary.path:
            budgets = boundary.budgets
            request.extensions[_EXTENSION] = budgets
        # HTTPX carries request extensions into redirect requests; auth retries
        # matching the original read also pass this hook. Writes are never tagged.
        if isinstance(budgets, RequestBudgets):
            budgets.spend()
    client.event_hooks["request"].append(count)
    _hooks[client] = count


@contextmanager
def observed_request(client: httpx.AsyncClient, method: str, path: str) -> Iterator[None]:
    limits = _limits.get()
    if not limits:
        yield
        return
    for limit in limits:
        limit.check()
    _install_hook(client)
    token = _boundary.set(ReadBoundary(client, method, path, RequestBudgets(limits)))
    try:
        yield
        for limit in limits:
            limit.check_time()
    finally:
        _boundary.reset(token)
