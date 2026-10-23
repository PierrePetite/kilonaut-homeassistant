"""Finished charges that could not be delivered yet.

Before this existed, a charge that ended while Kilonaut was unreachable was gone:
one error line in the log, nothing else. A wallbox session ends exactly once, so
there is no later moment that would send it again.

Now every finished charge is written here first and removed only once the server
has accepted it. Sending again is safe at any age: each entry carries the
Idempotency-Key, and Kilonaut stores it with the session, so a repeat merges into
the existing charge instead of creating a second one.

Deliberately free of Home Assistant imports. The storage is passed in as two
callables, so the logic can be tested without a running Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

# A queue that grows without bound would mean something is wrong for good — a revoked
# token, an account that is gone. Keep enough for weeks of daily charging, then drop
# the oldest rather than fill the user's disk.
MAX_ITEMS = 100

# Older than this and the user has long since noticed and entered it by hand.
MAX_AGE_S = 30 * 24 * 3600

# What a queued charge must carry to be sendable. Checked on load, so a damaged entry
# is skipped once instead of failing every retry and blocking the queue behind it.
REQUIRED = ("started_at", "ended_at", "energy_kwh", "idempotency_key")

Payload = dict[str, Any]
Load = Callable[[], Awaitable[Any]]
Save = Callable[[dict[str, Any]], Awaitable[None]]
Send = Callable[[Payload], Awaitable[Any]]


@dataclass
class FlushResult:
    sent: int = 0
    dropped: int = 0
    remaining: int = 0


def is_retryable(err: Exception) -> bool:
    """Whether trying again later can succeed.

    Unreachable, timeouts, 5xx, 408 and 429 pass with time. So does 401: a revoked
    token is fixed by entering a new one, and the charges should still arrive then.
    Any other 4xx means the server read the charge and refuses it — sending the same
    thing again will be refused again.
    """
    return bool(getattr(err, "retryable", True))


class PendingSessions:
    """A small persistent FIFO of session payloads, keyed by Idempotency-Key."""

    def __init__(
        self,
        load: Load,
        save: Save,
        *,
        now: Callable[[], float] = time.time,
        max_items: int = MAX_ITEMS,
        max_age_s: float = MAX_AGE_S,
    ) -> None:
        self._load = load
        self._save = save
        self._now = now
        self._max_items = max_items
        self._max_age_s = max_age_s
        self._items: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._items)

    async def async_load(self) -> None:
        data = await self._load()
        items = data.get("items") if isinstance(data, dict) else None
        self._items = [
            i for i in (items or [])
            if isinstance(i, dict) and isinstance(i.get("payload"), dict)
            and all(i["payload"].get(k) not in (None, "") for k in REQUIRED)
        ]
        if items and len(self._items) != len(items):
            _LOGGER.warning("Kilonaut: %d damaged queue entr(ies) skipped", len(items) - len(self._items))

    async def add(self, payload: Payload) -> None:
        """Remember a finished charge. A second add with the same key replaces the first."""
        key = payload.get("idempotency_key")
        if not key:
            raise ValueError("A pending session needs an idempotency_key, or a retry could duplicate it.")

        async with self._lock:
            self._items = [i for i in self._items if i["payload"].get("idempotency_key") != key]
            self._items.append({"payload": dict(payload), "queued_at": self._now()})
            if len(self._items) > self._max_items:
                dropped = len(self._items) - self._max_items
                self._items = self._items[dropped:]
                _LOGGER.warning("Kilonaut: %d undelivered charge(s) dropped, queue is full", dropped)
            await self._persist()

    async def flush(self, send: Send) -> FlushResult:
        """Send what is waiting, oldest first.

        Stops at the first error that may pass with time: if the server is down, the
        next item will fail the same way, and hammering it helps nobody. A refused
        item is dropped and the flush goes on.
        """
        result = FlushResult()

        async with self._lock:
            cutoff = self._now() - self._max_age_s
            fresh = [i for i in self._items if i.get("queued_at", 0) >= cutoff]
            if len(fresh) != len(self._items):
                result.dropped += len(self._items) - len(fresh)
                _LOGGER.warning(
                    "Kilonaut: %d undelivered charge(s) older than %d days dropped",
                    len(self._items) - len(fresh), self._max_age_s // 86400,
                )
                self._items = fresh

            while self._items:
                item = self._items[0]
                try:
                    await send(item["payload"])
                except Exception as err:  # noqa: BLE001 — classification decides, not the type
                    if is_retryable(err):
                        _LOGGER.debug("Kilonaut: delivery still failing, will retry: %s", err)
                        break
                    _LOGGER.error("Kilonaut: charge refused by the server, not retrying: %s", err)
                    self._items.pop(0)
                    result.dropped += 1
                    continue
                self._items.pop(0)
                result.sent += 1

            if result.sent or result.dropped:
                await self._persist()
            result.remaining = len(self._items)

        return result

    async def _persist(self) -> None:
        # A copy: the store may write after this returns, and the list keeps changing.
        await self._save({"items": [dict(i) for i in self._items]})
