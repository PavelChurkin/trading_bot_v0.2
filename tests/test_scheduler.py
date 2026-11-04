"""Планировщик (п. 21): token bucket, PriorityQueue P0/P1/P2, ретраи, TTL-кэш."""

import asyncio
import time

import pytest

from app.scheduler import P0, P1, P2, RequestScheduler, RetryableError, TokenBucket, TTLCache


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


def test_priority_order_p0_first():
    async def main():
        sch = RequestScheduler(TokenBucket(1000, 1000))

        def job(v):
            async def f():
                return v
            return f

        futs = [sch.submit(P2, "bg", job("p2")), sch.submit(P1, "tail", job("p1")),
                sch.submit(P0, "detail", job("p0")), sch.submit(P1, "tail2", job("p1b"))]
        sch.start()
        res = await asyncio.gather(*futs)
        await sch.stop()
        return res, [n for _, n in sch.order_log]

    res, order = run(main())
    assert res == ["p2", "p1", "p0", "p1b"]
    assert order == ["detail", "tail", "tail2", "bg"]  # P2 никогда не лезет вперёд P0/P1


def test_dedup_returns_same_future_and_bumps_priority():
    async def main():
        sch = RequestScheduler(TokenBucket(1000, 1000))
        calls = []

        def job(v):
            async def f():
                calls.append(v)
                return v
            return f

        a = sch.submit(P2, "x as P2", job("x"), dedup_key="x")
        sch.submit(P1, "y", job("y"))
        b = sch.submit(P0, "x as P0", job("x2"), dedup_key="x")
        assert a is b
        assert sch.queue_lengths() == {"P0": 1, "P1": 1, "P2": 0}
        sch.start()
        await asyncio.gather(a, b)
        await asyncio.sleep(0.01)
        await sch.stop()
        return calls

    assert run(main()) == ["x", "y"]  # одна задача x, выполнена первой


def test_token_bucket_limits_rate():
    async def main():
        bucket = TokenBucket(rate=20, capacity=1)
        t0 = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        return time.monotonic() - t0

    assert run(main()) >= 4 / 20 * 0.9


def test_token_bucket_refill_with_fake_clock():
    now = [0.0]
    bucket = TokenBucket(rate=1, capacity=1, clock=lambda: now[0])
    run(bucket.acquire())
    bucket._refill()
    assert bucket._tokens == pytest.approx(0)
    now[0] = 0.5
    bucket._refill()
    assert bucket._tokens == pytest.approx(0.5)
    now[0] = 10
    bucket._refill()
    assert bucket._tokens == 1  # не больше ёмкости


def test_retry_on_429_with_backoff_and_metrics():
    async def main():
        sch = RequestScheduler(TokenBucket(1000, 1000), backoff_base=0.01)
        attempts = []

        async def flaky():
            attempts.append(time.monotonic())
            if len(attempts) < 3:
                sch.record_request(5.0, 429)
                raise RetryableError("HTTP 429", status=429)
            sch.record_request(7.0, 200)
            return "ok"

        sch.start()
        res = await sch.submit(P1, "flaky", flaky)
        m = sch.metrics()
        await sch.stop()
        return res, attempts, m

    res, attempts, m = run(main())
    assert res == "ok" and len(attempts) == 3
    assert attempts[2] - attempts[1] >= attempts[1] - attempts[0]  # экспоненциальная пауза
    assert m["retries"] == 2
    assert m["http_429_last_hour"] >= 2
    assert m["avg_response_ms"] == pytest.approx((5 + 5 + 7) / 3, rel=0.01)
    assert set(m["queue"]) == {"P0", "P1", "P2"} and "p1_lag_sec" in m


def test_retry_gives_up():
    async def main():
        sch = RequestScheduler(TokenBucket(1000, 1000), max_attempts=2, backoff_base=0.01)

        async def bad():
            raise RetryableError("HTTP 503", status=503)

        sch.start()
        with pytest.raises(RetryableError):
            await sch.submit(P0, "bad", bad)
        f = sch.failed
        await sch.stop()
        return f

    assert run(main()) == 1


def test_p1_lag_counts_waiting_jobs():
    async def main():
        sch = RequestScheduler(TokenBucket(1000, 1000))

        async def f():
            return 1

        fut = sch.submit(P1, "tail", f)
        await asyncio.sleep(0.05)
        lag = sch.p1_lag()
        sch.start()
        await fut
        await sch.stop()
        return lag

    assert run(main()) >= 0.04


def test_ttl_cache_expiry():
    now = [0.0]
    c = TTLCache(max_items=2, clock=lambda: now[0])
    c.set("a", 1, 10)
    assert c.get("a") == 1 and c.hits == 1
    now[0] = 11
    assert c.get("a") is None and c.misses == 1
    c.set("a", 1, 10)
    c.set("b", 2, 10)
    c.set("c", 3, 10)  # переполнение — вытесняется самый старый
    assert len(c) == 2 and c.get("a") is None
