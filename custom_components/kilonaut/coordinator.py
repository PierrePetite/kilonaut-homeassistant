"""Charge detection + push logic for the Kilonaut integration.

Watches the configured power and energy sensors, lets detector.py decide when a
charge starts, pauses and ends, and pushes everything to Kilonaut's REST API.
Optionally pushes the odometer.

A finished charge is queued on disk before it is sent and removed only once
Kilonaut accepted it (see pending.py), so a charge that ends while the server is
unreachable is delivered later instead of lost.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
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
    STOP_GRACE_S,
)
from .detector import ChargeDetector, Event as ChargeEvent
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
        self._location_type: str = options.get(CONF_LOCATION_TYPE, DEFAULT_LOCATION_TYPE)
        self._detector = ChargeDetector(
            threshold_w=float(options.get(CONF_THRESHOLD_W, DEFAULT_THRESHOLD_W)),
            grace_s=STOP_GRACE_S,
        )

        self._unsub_state = None
        self._unsub_energy = None
        self._unsub_heartbeat = None
        self._unsub_stop = None
        self._unsub_odometer = None
        self._unsub_retry = None

    async def async_start(self) -> None:
        """Begin watching the power sensor, and send anything left from before."""
        await self._pending.async_load()
        # The energy counter is followed on its own, so a charge that starts while it
        # is briefly unavailable still has the last valid reading to start from.
        self._detector.energy(self._energy_kwh())
        self._unsub_energy = async_track_state_change_event(
            self.hass, [self._energy_entity], self._handle_energy_event
        )
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
        for unsub in (
            self._unsub_state, self._unsub_energy, self._unsub_heartbeat,
            self._unsub_stop, self._unsub_odometer, self._unsub_retry,
        ):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_energy = self._unsub_heartbeat = None
        self._unsub_stop = self._unsub_odometer = self._unsub_retry = None

    # --- power handling -----------------------------------------------------

    @callback
    def _handle_energy_event(self, event: Event) -> None:
        self._detector.energy(self._energy_kwh())

    @callback
    def _handle_power_event(self, event: Event) -> None:
        self.hass.async_create_task(self._process())

    async def _process(self) -> None:
        power_w = self._power_w()
        # Read here too, not only on its own event: when both sensors come back
        # together, the power event may be handled first.
        self._detector.energy(self._energy_kwh())
        match self._detector.power(power_w, dt_util.utcnow()):
            case ChargeEvent.START:
                await self._on_start(power_w)
            case ChargeEvent.PAUSE:
                _LOGGER.debug("Kilonaut: power below threshold, ending in %ds unless it returns", STOP_GRACE_S)
                self._unsub_stop = async_call_later(self.hass, STOP_GRACE_S, self._on_grace_expired)
            case ChargeEvent.RESUME:
                _LOGGER.debug("Kilonaut: power back, same charge continues")
                self._cancel_stop_timer()

    async def _on_start(self, power_w: float) -> None:
        _LOGGER.debug("Kilonaut: charge started (%.0f W)", power_w)
        await self._send_status(True, power_w, self._detector.session_kwh())
        self._unsub_heartbeat = async_track_time_interval(
            self.hass, self._heartbeat, timedelta(seconds=HEARTBEAT_INTERVAL_S)
        )

    async def _heartbeat(self, _now) -> None:
        # Keeps running through a pause, so Kilonaut does not drop the live tile.
        self._detector.energy(self._energy_kwh())
        power_w = self._power_w() or 0.0
        await self._send_status(True, power_w, self._detector.session_kwh())

    def _cancel_stop_timer(self) -> None:
        if self._unsub_stop:
            self._unsub_stop()
            self._unsub_stop = None

    async def _on_grace_expired(self, _now) -> None:
        self._unsub_stop = None
        await self._on_stop()

    async def _on_stop(self) -> None:
        if self._unsub_heartbeat:
            self._unsub_heartbeat()
            self._unsub_heartbeat = None
        self._detector.energy(self._energy_kwh())
        charge = self._detector.finish()
        await self._send_status(False, 0.0)
        if charge is None:
            return

        kwh = charge.energy_kwh
        if kwh is None:
            # Not "too small" — unknown. Said out loud, because the charge is lost.
            _LOGGER.warning(
                "Kilonaut: charge %s – %s ended without a reading from %s, not sent",
                charge.started_at.isoformat(), charge.ended_at.isoformat(), self._energy_entity,
            )
            return
        if kwh < MIN_SESSION_KWH:
            _LOGGER.debug("Kilonaut: charge ended, but delta too small (%s) — skipped", kwh)
            return

        # Written to disk first, sent second: a charge that fails to send — or a Home
        # Assistant that restarts mid-request — is retried instead of lost.
        await self._pending.add({
            "started_at": charge.started_at.isoformat(),
            "ended_at": charge.ended_at.isoformat(),
            "energy_kwh": kwh,
            "location_type": self._location_type,
            "charge_type": "ac",
            "idempotency_key": f"ha-{int(charge.started_at.timestamp())}",
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
                started_at=self._detector.started_at.isoformat() if self._detector.started_at else None,
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
