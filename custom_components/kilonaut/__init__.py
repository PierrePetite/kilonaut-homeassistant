"""The Kilonaut integration.

Runs entirely on the user's Home Assistant: it watches a wallbox power/energy
sensor and pushes charging sessions (and optionally the odometer) to Kilonaut's
free REST API. No cloud component of ours is involved beyond that HTTP API.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KilonautApiClient
from .const import CONF_API_BASE, CONF_API_TOKEN, DEFAULT_API_BASE
from .coordinator import KilonautChargeManager

_LOGGER = logging.getLogger(__name__)

type KilonautConfigEntry = ConfigEntry[KilonautChargeManager]


async def async_setup_entry(hass: HomeAssistant, entry: KilonautConfigEntry) -> bool:
    """Set up Kilonaut from a config entry."""
    session = async_get_clientsession(hass)
    client = KilonautApiClient(
        session,
        entry.data[CONF_API_TOKEN],
        entry.data.get(CONF_API_BASE, DEFAULT_API_BASE),
    )

    # Options (entities/threshold) may be edited later; fall back to entry data.
    options = {**entry.data, **entry.options}
    manager = KilonautChargeManager(hass, client, options)
    await manager.async_start()

    entry.runtime_data = manager
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KilonautConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.runtime_data:
        entry.runtime_data.async_stop()
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: KilonautConfigEntry) -> None:
    """Reload when the user changes options (entities, threshold, …)."""
    await hass.config_entries.async_reload(entry.entry_id)
