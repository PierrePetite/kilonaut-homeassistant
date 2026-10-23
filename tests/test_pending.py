"""Tests for the undelivered-charges queue (custom_components/kilonaut/pending.py).

Loaded by file path rather than as a package import: the package's __init__ pulls
in Home Assistant, and this module deliberately does not need it.

Run: python -m pytest tests
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "kilonaut" / "pending.py"
_spec = importlib.util.spec_from_file_location("kilonaut_pending", _PATH)
pending = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pending)


class FakeStore:
    """Stands in for homeassistant.helpers.storage.Store."""

    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def load(self):
        return self.data

    async def save(self, data):
        self.data = {"items": [dict(i) for i in data["items"]]}
        self.saves += 1


class ApiError(Exception):
    """Mirrors api.KilonautApiError's retry decision without importing aiohttp."""

    def __init__(self, status=None):
        super().__init__(f"status {status}")
        self.status = status

    @property
    def retryable(self):
        return self.status is None or self.status >= 500 or self.status in (401, 408, 429)


class Server:
    """Records what arrives; fails while `down` is set, or refuses with `refuse`."""

    def __init__(self):
        self.received = []
        self.down = False
        self.refuse = {}

    async def send(self, payload):
        if self.down:
            raise ApiError(None)
        if payload["idempotency_key"] in self.refuse:
            raise ApiError(self.refuse[payload["idempotency_key"]])
        self.received.append(payload["idempotency_key"])


def charge(key):
    return {
        "started_at": "2026-10-23T18:00:00+00:00",
        "ended_at": "2026-10-23T21:00:00+00:00",
        "energy_kwh": 22.5,
        "location_type": "home",
        "charge_type": "ac",
        "idempotency_key": key,
    }


def make(store=None, clock=None, **kw):
    store = store or FakeStore()
    clock = clock or [1_000_000.0]
    queue = pending.PendingSessions(store.load, store.save, now=lambda: clock[0], **kw)
    return queue, store, clock


def run(coro):
    return asyncio.run(coro)


def test_a_charge_that_fails_to_send_is_kept_and_delivered_later():
    queue, store, _ = make()
    server = Server()

    async def scenario():
        await queue.async_load()
        server.down = True
        await queue.add(charge("ha-1"))
        first = await queue.flush(server.send)
        server.down = False
        second = await queue.flush(server.send)
        return first, second

    first, second = run(scenario())
    assert (first.sent, first.remaining) == (0, 1)
    assert (second.sent, second.remaining) == (1, 0)
    assert server.received == ["ha-1"]
    assert store.data == {"items": []}


def test_the_queue_survives_a_restart():
    store = FakeStore()
    server = Server()
    server.down = True

    async def before_restart():
        queue, _, _ = make(store)
        await queue.async_load()
        await queue.add(charge("ha-1"))
        await queue.flush(server.send)

    async def after_restart():
        queue, _, _ = make(store)
        await queue.async_load()
        server.down = False
        return len(queue), await queue.flush(server.send)

    run(before_restart())
    waiting, result = run(after_restart())
    assert waiting == 1
    assert result.sent == 1
    assert server.received == ["ha-1"]


def test_it_is_written_to_disk_before_it_is_sent():
    """A Home Assistant that dies mid-request must still have the charge on disk."""
    queue, store, _ = make()

    async def scenario():
        await queue.async_load()
        await queue.add(charge("ha-1"))

    run(scenario())
    assert [i["payload"]["idempotency_key"] for i in store.data["items"]] == ["ha-1"]


def test_order_is_kept_and_a_down_server_is_not_hammered():
    queue, _, _ = make()
    server = Server()
    calls = []

    async def counting_send(payload):
        calls.append(payload["idempotency_key"])
        await server.send(payload)

    async def scenario():
        await queue.async_load()
        server.down = True
        for key in ("ha-1", "ha-2", "ha-3"):
            await queue.add(charge(key))
        calls.clear()
        await queue.flush(counting_send)
        attempts_while_down = list(calls)
        server.down = False
        await queue.flush(counting_send)
        return attempts_while_down

    attempts_while_down = run(scenario())
    assert attempts_while_down == ["ha-1"]
    assert server.received == ["ha-1", "ha-2", "ha-3"]


def test_a_refused_charge_is_dropped_and_the_rest_still_goes_out():
    queue, _, _ = make()
    server = Server()
    server.refuse = {"ha-1": 422}

    async def scenario():
        await queue.async_load()
        await queue.add(charge("ha-1"))
        await queue.add(charge("ha-2"))
        return await queue.flush(server.send)

    result = run(scenario())
    assert (result.sent, result.dropped, result.remaining) == (1, 1, 0)
    assert server.received == ["ha-2"]


def test_a_revoked_token_keeps_the_charge_for_when_it_is_fixed():
    queue, _, _ = make()
    server = Server()
    server.refuse = {"ha-1": 401}

    async def scenario():
        await queue.async_load()
        await queue.add(charge("ha-1"))
        first = await queue.flush(server.send)
        server.refuse = {}
        second = await queue.flush(server.send)
        return first, second

    first, second = run(scenario())
    assert (first.dropped, first.remaining) == (0, 1)
    assert second.sent == 1


def test_the_same_charge_added_twice_is_queued_once():
    queue, _, _ = make()
    server = Server()

    async def scenario():
        await queue.async_load()
        server.down = True
        await queue.add(charge("ha-1"))
        await queue.add(charge("ha-1"))
        return len(queue)

    assert run(scenario()) == 1


def test_a_payload_without_idempotency_key_is_rejected():
    queue, _, _ = make()
    payload = charge("x")
    del payload["idempotency_key"]

    async def scenario():
        await queue.async_load()
        await queue.add(payload)

    try:
        run(scenario())
    except ValueError:
        return
    raise AssertionError("a charge without a key could be duplicated on retry")


def test_old_charges_expire():
    queue, _, clock = make(max_age_s=10 * 86400)
    server = Server()

    async def scenario():
        await queue.async_load()
        await queue.add(charge("ha-old"))
        clock[0] += 11 * 86400
        await queue.add(charge("ha-new"))
        return await queue.flush(server.send)

    result = run(scenario())
    assert result.dropped == 1
    assert server.received == ["ha-new"]


def test_a_full_queue_drops_the_oldest():
    queue, _, _ = make(max_items=3)

    async def scenario():
        await queue.async_load()
        for n in range(5):
            await queue.add(charge(f"ha-{n}"))
        return [i["payload"]["idempotency_key"] for i in queue._items]

    assert run(scenario()) == ["ha-2", "ha-3", "ha-4"]


def test_damaged_storage_does_not_break_startup():
    no_start = charge("ha-2")
    del no_start["started_at"]
    store = FakeStore({"items": [
        {"payload": {"no": "key"}},
        "garbage",
        {"payload": no_start, "queued_at": 1_000_000.0},
        {"payload": charge("ha-1"), "queued_at": 1_000_000.0},
    ]})
    queue, _, _ = make(store)

    run(queue.async_load())
    assert len(queue) == 1


def test_empty_storage_on_first_start():
    queue, _, _ = make(FakeStore(None))
    run(queue.async_load())
    assert len(queue) == 0


def test_the_real_api_error_classifies_like_the_fake():
    """The fake above must not drift from api.KilonautApiError."""
    source = (_PATH.parent / "api.py").read_text()
    assert "self.status is None or self.status >= 500 or self.status in (401, 408, 429)" in source
