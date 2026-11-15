"""Asynchronous HTTP client shared by the data sources: request pacing, retries and per-provider metrics.

Pacing is enforced before every request, so a provider's free allowance is respected however many
series are fetched concurrently. A `Retry-After` header is obeyed. Error messages never contain the
request URL or its parameters, because API keys travel there.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx


class SourceError(Exception):
    """A message that is safe to show to the user (never contains a key)."""


class SourceAuthError(SourceError):
    pass


class SourceRateLimited(SourceError):
    pass


class SourceNotFound(SourceError):
    pass


class SourceDisabled(SourceError):
    """The source is switched off, or the credentials it needs have not been supplied."""


class BudgetExhausted(SourceRateLimited):
    """The daily or monthly request budget set for a free plan is used up."""


class Pacer:
    """Spaces requests at least `interval` seconds apart. Safe to share between concurrent tasks."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.interval, self._clock, self._sleep = max(0.0, interval), clock, sleep
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.waited = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                self.waited += delay
                await self._sleep(delay)
                now = self._next
            self._next = now + self.interval

    def penalise(self, seconds: float) -> None:
        """Push the next request back, after the provider asked for a pause."""
        self._next = max(self._next, self._clock() + seconds)


@dataclass
class RequestStats:
    """What an ingestion asked of a provider. Persisted with each run, so availability and latency can be
    reported over time. `add` also counts towards the series being fetched by the current task (see `scoped`),
    so requests are attributed correctly when several series share one connection."""

    requests: int = 0
    errors: int = 0
    rate_limited: int = 0
    retries: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def add(self, name: str, amount: int = 1) -> None:
        setattr(self, name, getattr(self, name) + amount)
        scope = _SCOPE.get()
        if scope is not None and scope is not self:
            setattr(scope, name, getattr(scope, name) + amount)

    def timed(self, milliseconds: float) -> None:
        self.latency_ms.append(milliseconds)
        scope = _SCOPE.get()
        if scope is not None and scope is not self:
            scope.latency_ms.append(milliseconds)

    def to_dict(self) -> dict:
        lat = sorted(self.latency_ms)
        return {"requests": self.requests, "errors": self.errors, "rate_limited": self.rate_limited, "retries": self.retries,
                "latency_ms_median": round(lat[len(lat) // 2], 1) if lat else None, "latency_ms_max": round(lat[-1], 1) if lat else None}


_SCOPE: ContextVar[RequestStats | None] = ContextVar("datahub_request_scope", default=None)


def scoped() -> RequestStats:
    """Start counting the current task's requests separately. Returns the counter."""
    stats = RequestStats()
    _SCOPE.set(stats)
    return stats


class ApiClient:
    """One provider's connection. `budget` is called before each request and may raise BudgetExhausted."""

    def __init__(self, name: str, base_url: str, interval: float, headers: dict | None = None, params: dict | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0, max_retries: int = 3,
                 budget: Callable[[], None] | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.base_url = name, base_url.rstrip("/")
        self._headers, self._params = headers or {}, params or {}
        self._transport, self._timeout, self._max_retries = transport, timeout, max_retries
        self._budget, self._sleep, self._clock = budget, sleep, clock
        self.pacer = Pacer(interval, clock, sleep)
        self.stats = RequestStats()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ApiClient":
        self._client = httpx.AsyncClient(transport=self._transport, timeout=self._timeout, headers=self._headers)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_json(self, path: str, params: dict | None = None, base_url: str | None = None):
        """GET and decode JSON. Retries timeouts, 429 and 5xx with backoff; raises a classified SourceError otherwise."""
        if self._client is None:
            raise RuntimeError("ApiClient must be used as an async context manager")
        url = f"{(base_url or self.base_url).rstrip('/')}/{path.lstrip('/')}"
        label = self.name
        last: SourceError | None = None
        for attempt in range(self._max_retries + 1):
            if self._budget is not None:
                self._budget()
            await self.pacer.wait()
            started = self._clock()
            self.stats.add("requests")
            try:
                response = await self._client.get(url, params={**self._params, **(params or {})})
            except httpx.HTTPError as exc:  # the exception text can contain the URL with the key
                self.stats.add("errors")
                last = SourceError(f"{label} could not be reached ({type(exc).__name__})")
                last.__cause__, last.__suppress_context__ = None, True
            else:
                self.stats.timed((self._clock() - started) * 1000)
                code = response.status_code
                if code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        self.stats.add("errors")
                        raise SourceError(f"{label} returned a response that is not JSON") from None
                self.stats.add("errors")
                if code in (401, 403):
                    raise SourceAuthError(f"{label} rejected the credentials, or the plan does not include this data (HTTP {code})")
                if code == 404:
                    raise SourceNotFound(f"{label} has no data for this request")
                if code in (418, 429):
                    self.stats.add("rate_limited")
                    pause = _retry_after(response) or min(60.0, 5.0 * 2 ** attempt)
                    self.pacer.penalise(pause)
                    last = SourceRateLimited(f"{label}: too many requests")
                    if code == 418:  # an address ban: retrying only prolongs it
                        raise last
                elif code >= 500:
                    last = SourceError(f"{label} answered with HTTP {code}")
                else:
                    raise SourceError(f"{label} refused the request (HTTP {code})")
            if attempt < self._max_retries:
                self.stats.add("retries")
                if not isinstance(last, SourceRateLimited):
                    await self._sleep(min(30.0, 1.0 * 2 ** attempt))
        assert last is not None
        raise last


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, min(float(value), 3600.0)) if value else None
    except ValueError:
        return None
