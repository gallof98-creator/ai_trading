"""The shared HTTP client: pacing, retries, error classification, and that keys never leak."""
import asyncio

import httpx
import pytest

from app.datahub.http import ApiClient, BudgetExhausted, Pacer, SourceAuthError, SourceError, SourceNotFound, SourceRateLimited

KEY = "secret-key-123456"


class Clock:
    """A manual clock: sleeping advances it, so pacing can be checked without waiting."""

    def __init__(self) -> None:
        self.now, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def run(coro):
    return asyncio.run(coro)


def client(handler, clock: Clock, interval: float = 0.0, **kw) -> ApiClient:
    return ApiClient("Vendor", "https://vendor.example/api", interval, params={"api_key": KEY}, transport=httpx.MockTransport(handler),
                     sleep=clock.sleep, clock=clock, **kw)


def test_requests_are_spaced_by_the_interval_even_when_issued_concurrently():
    clock, starts = Clock(), []

    def handler(request):
        starts.append(clock.now)
        return httpx.Response(200, json={"n": len(starts)})

    async def go():
        async with client(handler, clock, interval=0.5) as c:
            return await asyncio.gather(*(c.get_json("x") for _ in range(5)))

    assert len(run(go())) == 5
    assert starts == [0.0, 0.5, 1.0, 1.5, 2.0]  # five simultaneous callers still leave the vendor 0.5 s between requests


def test_pacer_can_be_pushed_back():
    clock = Clock()
    pacer = Pacer(1.0, clock, clock.sleep)

    async def go():
        await pacer.wait()
        pacer.penalise(30)
        await pacer.wait()

    run(go())
    assert clock.now == 30 and pacer.waited == 30


def test_rate_limit_obeys_retry_after_then_succeeds():
    clock, codes = Clock(), [429, 429, 200]

    def handler(request):
        code = codes.pop(0)
        return httpx.Response(code, json={"ok": True}, headers={"Retry-After": "7"} if code == 429 else {})

    async def go():
        async with client(handler, clock) as c:
            out = await c.get_json("x")
            return out, c.stats

    out, stats = run(go())
    assert out == {"ok": True} and clock.slept == [7.0, 7.0]
    assert (stats.requests, stats.rate_limited, stats.retries, stats.errors) == (3, 2, 2, 2)


def test_rate_limit_that_persists_is_reported_and_a_ban_is_not_retried():
    clock = Clock()

    async def go(code):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(code)

        async with client(handler, clock, max_retries=2) as c:
            with pytest.raises(SourceRateLimited):
                await c.get_json("x")
        return len(calls)

    assert run(go(429)) == 3  # the first attempt and two retries
    assert run(go(418)) == 1  # an address ban: asking again only prolongs it


def test_server_errors_are_retried_with_backoff_and_client_errors_are_not():
    clock, codes = Clock(), [503, 500, 200]

    def flaky(request):
        return httpx.Response(codes.pop(0), json=[1])

    async def go(handler, **kw):
        async with client(handler, clock, **kw) as c:
            return await c.get_json("x")

    assert run(go(flaky)) == [1] and clock.slept == [1.0, 2.0]
    for code, exc in ((401, SourceAuthError), (403, SourceAuthError), (404, SourceNotFound), (400, SourceError)):
        calls = []

        def handler(request, code=code):
            calls.append(1)
            return httpx.Response(code)

        with pytest.raises(exc):
            run(go(handler))
        assert len(calls) == 1


def test_errors_never_contain_the_url_or_the_key():
    clock = Clock()

    def unreachable(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    async def go(handler):
        async with client(handler, clock, max_retries=1) as c:
            await c.get_json("series", {"id": "X"})

    for handler in (unreachable, lambda r: httpx.Response(200, text="<html>not json</html>"), lambda r: httpx.Response(500, text=f"error at {r.url}")):
        with pytest.raises(SourceError) as info:
            run(go(handler))
        assert KEY not in str(info.value) and "vendor.example" not in str(info.value)
        assert info.value.__cause__ is None


def test_budget_is_checked_before_every_request():
    clock, sent = Clock(), []

    def handler(request):
        sent.append(1)
        return httpx.Response(200, json={})

    def budget():
        if len(sent) >= 2:
            raise BudgetExhausted("Vendor: today's request budget (2) is used up")

    async def go():
        async with client(handler, clock, budget=budget) as c:
            await c.get_json("a")
            await c.get_json("b")
            await c.get_json("c")

    with pytest.raises(BudgetExhausted):
        run(go())
    assert len(sent) == 2  # the third request was never sent
