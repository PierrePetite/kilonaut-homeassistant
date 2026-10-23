"""Thin async client for the Kilonaut REST ingestion API.

Only talks to the public, free endpoints documented at
https://kilonaut.de/hilfe/api — nothing here needs a Kilonaut backend change.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import async_timeout

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


class KilonautApiError(Exception):
    """Raised when the Kilonaut API returns an error or is unreachable.

    `status` is the HTTP status, or None when the server was not reached at all.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """Whether the same request can succeed later (see pending.is_retryable)."""
        return self.status is None or self.status >= 500 or self.status in (401, 408, 429)


class KilonautAuthError(KilonautApiError):
    """Raised when the token is rejected (401)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=401)


class KilonautApiClient:
    """Minimal Kilonaut API client built on Home Assistant's aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession, token: str, base: str) -> None:
        self._session = session
        self._token = token
        self._base = base.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}{path}"
        headers = dict(self._headers)
        if extra_headers:
            headers.update(extra_headers)
        try:
            async with async_timeout.timeout(REQUEST_TIMEOUT):
                async with self._session.request(method, url, json=json, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 401:
                        raise KilonautAuthError("Kilonaut rejected the API token (401).")
                    if resp.status >= 400:
                        raise KilonautApiError(f"Kilonaut API {resp.status}: {body[:200]}", status=resp.status)
                    try:
                        return await resp.json()
                    except aiohttp.ContentTypeError:
                        return {}
        except (aiohttp.ClientError, TimeoutError) as err:
            raise KilonautApiError(f"Could not reach Kilonaut: {err}") from err

    async def ping(self) -> dict[str, Any]:
        """Verify the token and return the linked account."""
        return await self._request("GET", "/ping")

    async def push_status(
        self, *, charging: bool, energy_kwh: float | None = None,
        power_kw: float | None = None, started_at: str | None = None,
        location_type: str = "home",
    ) -> dict[str, Any]:
        """Send a live 'charging now' heartbeat (drives the dashboard tile)."""
        payload: dict[str, Any] = {"charging": charging, "location_type": location_type}
        if energy_kwh is not None:
            payload["energy_kwh"] = round(energy_kwh, 3)
        if power_kw is not None:
            payload["power_kw"] = round(power_kw, 3)
        if started_at is not None:
            payload["started_at"] = started_at
        return await self._request("POST", "/charging/status", json=payload)

    async def push_session(
        self, *, started_at: str, ended_at: str, energy_kwh: float,
        location_type: str = "home", charge_type: str = "ac",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a finished charging session."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self._request(
            "POST", "/sessions",
            json={
                "started_at": started_at,
                "ended_at": ended_at,
                "energy_kwh": round(energy_kwh, 3),
                "location_type": location_type,
                "charge_type": charge_type,
            },
            extra_headers=headers,
        )

    async def push_odometer(self, *, reading_km: int, recorded_at: str | None = None) -> dict[str, Any]:
        """Push an odometer reading (idempotent per vehicle + reading + day)."""
        payload: dict[str, Any] = {"reading_km": int(reading_km)}
        if recorded_at is not None:
            payload["recorded_at"] = recorded_at
        return await self._request("POST", "/odometer", json=payload)
