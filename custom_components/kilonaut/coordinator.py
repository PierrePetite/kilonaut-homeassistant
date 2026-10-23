"""Charge detection + push logic for the Kilonaut integration.

Watches the configured power sensor, detects charge start/stop from the power
draw, computes the session energy as the delta of the cumulative energy counter,
and pushes everything to Kilonaut's REST API. Optionally pushes the odometer.

A finished charge is queued on disk before it is sent and removed only once
Kilonaut accepted it (see pending.py), so a charge that ends while the server is
unreachable is delivered later instead of lost.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import KilonautApiClient, KilonautApiError
from .const import (
    CONF_ENERGY_ENTITY,
    CONF_LOCATION_TYPE,
    CONF_ODOMETER_ENTITY,
    CONF_POWER_ENTITY,
    CONF_THRESHOLD_W,
    DEFAULT_LOCATION_TYPE,
    DEFAULT_THRESHOLD_W,
    DOMAIN,
    HEARTBEAT_INTERVAL_S,
    MIN_SESSION_KWH,
    ODOMETER_INTERVAL_H,
    RETRY_INTERVAL_MIN,
)
from .pending import PendingSessions

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (None, "", "unknown", "unavailable")

STORAGE_VERSION = 1


def pending_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Where one config entry keeps its undelivered charges (.storage/kilonaut.pending.<id>)."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.pending.{entry_id}")


class KilonautChargeManager:
    """Turns wallbox power/energy readings into Kilonaut sessions."""

    def __init__(self, hass: HomeAssistant, client: KilonautApiClient, options: dict, entry_id: str) -> None:
        self.hass = hass
        self.client = client
        store = pending_store(hass, entry_id)
        self._pending = PendingSessions(
            load=store.async_load,
            save=store.async_save,
        )
        self._power_entity: str = options[CONF_POWER_ENTITY]
        self._energy_entity: str = options[CONF_ENERGY_ENTITY]
        self._odometer_entity: str | None = options.get(CONF_ODOMETER_ENTITY) or None
        self._threshold_w: float = float(options.get(CONF_THRESHOLD_W, DEFAULT_THRESHOLD_W))
        self._location_type: str = options.get(CONF_LOCATION_TYPE, DEFAULT_LOCATION_TYPE)

        self._charging = False
        self._start_energy_kwh: float | None = None
        self._started_at = None

        self._unsub_state = None
        self._unsub_heartbeat = None
        self._unsub_odometer = None
        self._unsub_retry = None

    async def async_start(self) -> None:
        """Begin watching the power sensor, and send anything left from before."""
        await self._pending.async_load()
        self._unsub_state = async_track_state_change_event(
            self.hass, [self._power_entity], self._handle_power_event
        )
        self._unsub_retry = async_track_time_interval(
            self.hass, self._retry_pending, timedelta(minutes=RETRY_INTERVAL_MIN)
        )
        if len(self._pending):
            self.hass.async_create_task(self._retry_pending())
        if self._odometer_entity:
            # Push the odometer regularly; the server dedups per day, so an
            # occasional extra push is harmless.
            self._unsub_odometer = async_track_time_interval(
                self.hass, self._push_odometer, timedelta(hours=ODOMETER_INTERVAL_H)
            )

    @callback
    def async_stop(self) -> None:
        """Tear down all listeners."""
        for unsub in (self._unsub_state, self._unsub_heartbeat, self._unsub_odometer, self._unsub_retry):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_heartbeat = self._unsub_odometer = self._unsub_retry = None

    # --- power handling -----------------------------------------------------

    @callback
    def _handle_power_event(self, event: Event) -> None:
        self.hass.async_create_task(self._process())

    async def _process(self) -> None:
        power_w = self._power_w()
        if power_w is None:
            return
        is_charging = power_w > self._threshold_w
        if is_charging and not self._charging:
            await self._on_start(power_w)
        elif not is_charging and self._charging:
            await self._on_stop()

    async def _on_start(self, power_w: float) -> None:
        self._charging = True
        self._start_energy_kwh = self._energy_kwh()
        self._started_at = dt_util.utcnow()
        _LOGGER.debug("Kilonaut: charge started (%.0f W)", power_w)
        await self._send_status(True, power_w, 0.0)
        self._unsub_heartbeat = async_track_time_interval(
            self.hass, self._heartbeat, timedelta(seconds=HEARTBEAT_INTERVAL_S)
        )

    async def _heartbeat(self, _now) -> None:
        power_w = self._power_w() or 0.0
        delta = self._session_kwh()
        await self._send_status(True, power_w, delta)

    async def _on_stop(self) -> None:
        if self._unsub_heartbeat:
            self._unsub_heartbeat()
            self._unsub_heartbeat = None
        self._charging = False
        await self._send_status(False, 0.0)

        kwh = self._session_kwh()
        ended_at = dt_util.utcnow()
        if kwh is None or kwh < MIN_SESSION_KWH:
            _LOGGER.debug("Kilonaut: charge ended, but delta too small (%s) — skipped", kwh)
            return

        # Written to disk first, sent second: a charge that fails to send — or a Home
        # Assistant that restarts mid-request — is retried instead of lost.
        await self._pending.add({
            "started_at": self._started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "energy_kwh": kwh,
            "location_type": self._location_type,
            "charge_type": "ac",
            "idempotency_key": f"ha-{int(self._started_at.timestamp())}",
        })
        await self._retry_pending()

        await self._push_odometer()

    async def _retry_pending(self, _now=None) -> None:
        """Send queued charges. Called after each charge and every few minutes."""
        if not len(self._pending):
            return
        result = await self._pending.flush(self._send_session)
        if result.sent:
            _LOGGER.info("Kilonaut: %d charge(s) delivered", result.sent)
        if result.remaining:
            _LOGGER.warning(
                "Kilonaut: %d charge(s) not delivered yet, retrying every %d min",
                result.remaining, RETRY_INTERVAL_MIN,
            )

    async def _send_session(self, payload: dict) -> None:
        await self.client.push_session(
            started_at=payload["started_at"],
            ended_at=payload["ended_at"],
            energy_kwh=payload["energy_kwh"],
            location_type=payload.get("location_type", self._location_type),
            charge_type=payload.get("charge_type", "ac"),
            idempotency_key=payload["idempotency_key"],
        )

    async def _send_status(self, charging: bool, power_w: float, delta_kwh: float | None = None) -> None:
        try:
            await self.client.push_status(
                charging=charging,
                energy_kwh=delta_kwh,
                power_kw=power_w / 1000.0,
                started_at=self._started_at.isoformat() if self._started_at else None,
                location_type=self._location_type,
            )
        except KilonautApiError as err:
            _LOGGER.debug("Kilonaut: status heartbeat failed: %s", err)

    async def _push_odometer(self, _now=None) -> None:
        if not self._odometer_entity:
            return
        km = self._number(self._odometer_entity)
        if km is None:
            return
        try:
            await self.client.push_odometer(reading_km=int(round(km)))
        except KilonautApiError as err:
            _LOGGER.debug("Kilonaut: odometer push failed: %s", err)

    # --- reading helpers (unit aware) ---------------------------------------

    def _session_kwh(self) -> float | None:
        energy = self._energy_kwh()
        if energy is None or self._start_energy_kwh is None:
            return None
        return round(energy - self._start_energy_kwh, 3)

    def _power_w(self) -> float | None:
        state = self.hass.states.get(self._power_entity)
        value = self._as_float(state)
        if value is None:
            return None
        if (state.attributes.get("unit_of_measurement") or "").lower() == "kw":
            value *= 1000.0
        return value

    def _energy_kwh(self) -> float | None:
        state = self.hass.states.get(self._energy_entity)
        value = self._as_float(state)
        if value is None:
            return None
        if (state.attributes.get("unit_of_measurement") or "").lower() == "wh":
            value /= 1000.0
        return value

    def _number(self, entity_id: str) -> float | None:
        return self._as_float(self.hass.states.get(entity_id))

    @staticmethod
    def _as_float(state: State | None) -> float | None:
        if state is None or state.state in _UNAVAILABLE:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None
