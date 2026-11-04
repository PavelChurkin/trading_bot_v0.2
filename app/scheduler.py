"""Планировщик запросов к ISS (п. 21).

* Один :class:`TokenBucket` на весь процесс (1 запрос/сек по умолчанию):
  все исходящие запросы к MOEX проходят через него.
* Одна :class:`asyncio.PriorityQueue` с тремя уровнями:
  P0 — по триггеру и кнопкам дашборда, P1 — регулярный обход, P2 — фон.
  Один исполнитель берёт задачу с наименьшим приоритетом, поэтому пока
  в очереди есть P0 — P1 и P2 стоят.
* Retry/backoff на 429 и 5xx: экспоненциальная пауза, задача возвращается
  в очередь с тем же приоритетом, счётчик попыток пишется в лог.
* :class:`TTLCache` — кэш ответов с TTL.
* Метрики планировщика для вкладки дашборда.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

P0, P1, P2 = 0, 1, 2
PRIORITY_NAMES = {P0: "P0", P1: "P1", P2: "P2"}


class RetryableError(Exception):
    """Ошибка, после которой задачу стоит повторить (429, 5xx, таймаут)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class TokenBucket:
    """Token bucket: ``rate`` токенов в секунду, ёмкость ``capacity``."""

    def __init__(self, rate: float = 1.0, capacity: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class TTLCache:
    def __init__(self, max_items: int = 2000, clock: Callable[[], float] = time.monotonic):
        self._data: dict[Any, tuple[float, Any]] = {}
        self._max = max_items
        self._clock = clock
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> Any:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        expires, value = item
        if expires < self._clock():
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: Any, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        if len(self._data) >= self._max:
            now = self._clock()
            for k in [k for k, (e, _) in self._data.items() if e < now]:
                self._data.pop(k, None)
            if len(self._data) >= self._max:
                self._data.pop(next(iter(self._data)))
        self._data[key] = (self._clock() + ttl, value)

    def __len__(self) -> int:
        return len(self._data)


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    job: "Job" = field(compare=False)


@dataclass
class Job:
    name: str
    priority: int
    fn: Callable[[], Awaitable[Any]]
    future: asyncio.Future
    dedup_key: Optional[str] = None
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)


class RequestScheduler:
    def __init__(self, bucket: Optional[TokenBucket] = None, max_attempts: int = 5, backoff_base: float = 2.0,
                 backoff_max: float = 120.0):
        self.bucket = bucket or TokenBucket(1.0, 1.0)
        self.queue: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._seq = itertools.count()
        self._pending: dict[str, Job] = {}
        self._waiting_retry: dict[int, int] = {P0: 0, P1: 0, P2: 0}
        self._worker: Optional[asyncio.Task] = None
        self._running = False
        self.current: Optional[str] = None
        # метрики
        self.request_times: deque[float] = deque(maxlen=5000)
        self.response_ms: deque[float] = deque(maxlen=200)
        self.too_many: deque[float] = deque(maxlen=5000)
        self.errors: deque[tuple[float, str]] = deque(maxlen=50)
        self.completed = {P0: 0, P1: 0, P2: 0}
        self.retries = 0
        self.failed = 0
        self.p1_cycle: dict[str, Any] = {}
        self.order_log: deque[tuple[int, str]] = deque(maxlen=200)  # для тестов и отладки

    # ------------------------------------------------------------- очередь
    def submit(self, priority: int, name: str, fn: Callable[[], Awaitable[Any]],
               dedup_key: Optional[str] = None) -> asyncio.Future:
        """Ставит задачу в очередь. Повторная задача с тем же ``dedup_key``
        не дублируется — возвращается future уже стоящей в очереди."""
        if dedup_key and dedup_key in self._pending:
            existing = self._pending[dedup_key]
            if priority < existing.priority:
                # повышаем приоритет: ставим ещё раз, старая запись отбросится
                existing.priority = priority
                self.queue.put_nowait(_Item(priority, next(self._seq), existing))
            return existing.future
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        job = Job(name=name, priority=priority, fn=fn, future=fut, dedup_key=dedup_key)
        if dedup_key:
            self._pending[dedup_key] = job
        self.queue.put_nowait(_Item(priority, next(self._seq), job))
        return fut

    def p1_lag(self) -> float:
        """Лаг P1: сколько секунд ждёт самая старая задача регулярного обхода."""
        now = time.monotonic()
        waits = [now - item.job.enqueued_at for item in list(self.queue._queue)  # type: ignore[attr-defined]
                 if item.priority == P1 and item.job.priority == P1 and not item.job.future.done()]
        return max(waits, default=0.0)

    def queue_lengths(self) -> dict[str, int]:
        counts = {P0: 0, P1: 0, P2: 0}
        for item in list(self.queue._queue):  # type: ignore[attr-defined]
            if not item.job.future.done() and item.priority == item.job.priority:
                counts[item.priority] += 1
        for p, n in self._waiting_retry.items():
            counts[p] += n
        return {PRIORITY_NAMES[p]: n for p, n in counts.items()}

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._running = True
            self._worker = asyncio.create_task(self._run(), name="request-scheduler")

    async def stop(self) -> None:
        self._running = False
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if not item.job.future.done():
                item.job.future.cancel()
        self._pending.clear()  # иначе повторный submit с тем же dedup_key вернёт отменённый future

    async def _run(self) -> None:
        while self._running:
            item = await self.queue.get()
            job = item.job
            if job.future.done() or item.priority != job.priority:
                continue  # отменена или перепоставлена с другим приоритетом
            self.current = f"{PRIORITY_NAMES[job.priority]} {job.name}"
            self.order_log.append((job.priority, job.name))
            try:
                result = await job.fn()
            except RetryableError as exc:
                self._retry(job, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Задача %s упала: %s", job.name, exc)
                self.failed += 1
                self.errors.append((time.time(), f"{job.name}: {exc}"))
                self._finish(job, exc=exc)
            else:
                self.completed[job.priority] += 1
                self._finish(job, result=result)
            finally:
                self.current = None

    def _finish(self, job: Job, result: Any = None, exc: Optional[BaseException] = None) -> None:
        if job.dedup_key and self._pending.get(job.dedup_key) is job:
            self._pending.pop(job.dedup_key, None)
        if job.future.done():
            return
        if exc is not None:
            job.future.set_exception(exc)
            job.future.exception()  # помечаем как полученное, чтобы не было warning
        else:
            job.future.set_result(result)

    def _retry(self, job: Job, exc: RetryableError) -> None:
        job.attempts += 1
        if exc.status == 429:
            self.too_many.append(time.time())
        if job.attempts >= self.max_attempts:
            logger.error("Задача %s: %s попыток исчерпано (%s)", job.name, job.attempts, exc)
            self.failed += 1
            self.errors.append((time.time(), f"{job.name}: {exc}"))
            self._finish(job, exc=exc)
            return
        delay = min(self.backoff_base * (2 ** (job.attempts - 1)), self.backoff_max)
        self.retries += 1
        logger.warning("Задача %s: попытка %s не удалась (%s), повтор через %.1f с", job.name, job.attempts, exc, delay)
        self._waiting_retry[job.priority] += 1

        async def requeue() -> None:
            try:
                await asyncio.sleep(delay)
            finally:
                self._waiting_retry[job.priority] -= 1
            if not job.future.done():
                self.queue.put_nowait(_Item(job.priority, next(self._seq), job))

        asyncio.get_event_loop().create_task(requeue())

    # ------------------------------------------------------------- метрики
    def record_request(self, duration_ms: float, status: Optional[int]) -> None:
        self.request_times.append(time.time())
        self.response_ms.append(duration_ms)
        if status == 429:
            self.too_many.append(time.time())

    def metrics(self) -> dict[str, Any]:
        now = time.time()
        last_min = [t for t in self.request_times if now - t <= 60]
        return {
            "queue": self.queue_lengths(),
            "current": self.current,
            "requests_per_sec": round(len(last_min) / 60, 3),
            "requests_last_min": len(last_min),
            "requests_total": len(self.request_times),
            "avg_response_ms": round(sum(self.response_ms) / len(self.response_ms), 1) if self.response_ms else None,
            "http_429_last_hour": sum(1 for t in self.too_many if now - t <= 3600),
            "completed": {PRIORITY_NAMES[p]: n for p, n in self.completed.items()},
            "retries": self.retries,
            "failed": self.failed,
            "p1_lag_sec": round(self.p1_lag(), 1),
            "p1_cycle": self.p1_cycle,
            "rate_limit_rps": self.bucket.rate,
            "recent_errors": [{"ts": t, "error": e} for t, e in list(self.errors)[-10:]],
        }
