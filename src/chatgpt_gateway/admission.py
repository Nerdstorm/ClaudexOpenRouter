"""Bounded in-memory waiting shared by every model transport."""

import asyncio
from dataclasses import dataclass, field

from .config import Settings
from .errors import GatewayError


@dataclass
class Lease:
    deadline: float
    wait_ms: int
    _slots: list[asyncio.Semaphore] = field(repr=False)
    _owner: "Admission" = field(repr=False)

    def release(self) -> None:
        if self._slots:
            for slot in reversed(self._slots):
                slot.release()
            self._slots.clear()
            self._owner.active -= 1


class Admission:
    def __init__(self, settings: Settings):
        self.timeout = settings.admission_timeout_seconds
        self.max_waiting = settings.max_queued_requests
        self.waiting = 0
        self.active = 0

    async def acquire(self, *slots: asyncio.Semaphore, timeout_seconds: float) -> Lease:
        # Each narrower pool precedes the shared pool, so waiting images/native calls
        # never reserve shared capacity while their own pool is full.
        queued = any(slot.locked() for slot in slots)
        if queued and self.waiting >= self.max_waiting:
            raise GatewayError(
                429, "The gateway waiting queue is full.", "gateway_busy", retry_after="1"
            )
        if queued:
            self.waiting += 1
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout_seconds
        acquired = []
        try:
            async with asyncio.timeout_at(min(deadline, started + self.timeout)):
                for slot in slots:
                    await slot.acquire()
                    acquired.append(slot)
            self.active += 1
            return Lease(deadline, round((loop.time() - started) * 1000), acquired, self)
        except BaseException as error:
            for slot in reversed(acquired):
                slot.release()
            if isinstance(error, TimeoutError):
                raise GatewayError(
                    429,
                    "No gateway capacity became available before the waiting deadline.",
                    "gateway_busy",
                    retry_after="1",
                ) from error
            raise
        finally:
            if queued:
                self.waiting -= 1
