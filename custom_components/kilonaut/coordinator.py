"""Charge detection + push logic for the Kilonaut integration.

Watches the configured power sensor, detects charge start/stop from the power
draw, computes the session energy as the delta of the cumulative energy counter,
and pushes everything to Kilonaut's REST API. Optionally pushes the odometer.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
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
    HEARTBEAT_INTERVAL_S,
    MIN_SESSION_KWH,
    ODOMETER_INTERVAL_H,
)

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (None, "", "unknown", "unavailable")


class KilonautChargeManager:
    """Turns wallbox power/energy readings into Kilonaut sessions."""

    def __init__(self, hass: HomeAssistant, client: KilonautApiClient, options: dict) -> None:
        self.hass = hass
        self.client = client
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

    async def async_start(self) -> None:
        """Begin watching the power sensor."""
        self._unsub_state = async_track_state_change_event(
            self.hass, [self._power_entity], self._handle_power_event
        )
        if self._odometer_entity:
            # Push the odometer regularly; the server dedups per day, so an
            # occasional extra push is harmless.
            self._unsub_odometer = async_track_time_interval(
                self.hass, self._push_odometer, timedelta(hours=ODOMETER_INTERVAL_H)
            )

    @callback
    def async_stop(self) -> None:
        """Tear down all listeners."""
        for unsub in (self._unsub_state, self._unsub_heartbeat, self._unsub_odometer):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_heartbeat = self._unsub_odometer = None

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

        try:
            await self.client.push_session(
                started_at=self._started_at.isoformat(),
                ended_at=ended_at.isoformat(),
                energy_kwh=kwh,
                location_type=self._location_type,
                charge_type="ac",
                idempotency_key=f"ha-{int(self._started_at.timestamp())}",
            )
            _LOGGER.info("Kilonaut: session pushed (%.3f kWh)", kwh)
        except KilonautApiError as err:
            _LOGGER.error("Kilonaut: failed to push session: %s", err)

        await self._push_odometer()

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
