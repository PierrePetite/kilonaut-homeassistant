"""Config + options flow for the Kilonaut integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    ConfigEntry,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import KilonautApiClient, KilonautAuthError, KilonautApiError
from .const import (
    CONF_API_BASE,
    CONF_API_TOKEN,
    CONF_ENERGY_ENTITY,
    CONF_LOCATION_TYPE,
    CONF_ODOMETER_ENTITY,
    CONF_POWER_ENTITY,
    CONF_THRESHOLD_W,
    DEFAULT_API_BASE,
    DEFAULT_LOCATION_TYPE,
    DEFAULT_THRESHOLD_W,
    DOMAIN,
    LOCATION_TYPES,
)

_LOGGER = logging.getLogger(__name__)


def _entities_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for picking the wallbox sensors + options."""
    return vol.Schema(
        {
            vol.Required(
                CONF_POWER_ENTITY, default=defaults.get(CONF_POWER_ENTITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Required(
                CONF_ENERGY_ENTITY, default=defaults.get(CONF_ENERGY_ENTITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
            vol.Optional(
                CONF_ODOMETER_ENTITY,
                description={"suggested_value": defaults.get(CONF_ODOMETER_ENTITY)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_THRESHOLD_W,
                default=defaults.get(CONF_THRESHOLD_W, DEFAULT_THRESHOLD_W),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100, max=25000, step=100, unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_LOCATION_TYPE,
                default=defaults.get(CONF_LOCATION_TYPE, DEFAULT_LOCATION_TYPE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=LOCATION_TYPES, translation_key="location_type"
                )
            ),
        }
    )


class KilonautConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial UI setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: the API token (validated against /ping)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            base = user_input.get(CONF_API_BASE, DEFAULT_API_BASE) or DEFAULT_API_BASE
            client = KilonautApiClient(
                async_get_clientsession(self.hass), user_input[CONF_API_TOKEN], base
            )
            try:
                info = await client.ping()
            except KilonautAuthError:
                errors["base"] = "invalid_auth"
            except KilonautApiError:
                errors["base"] = "cannot_connect"
            else:
                email = (info.get("user") or {}).get("email")
                await self.async_set_unique_id(email or user_input[CONF_API_TOKEN][:12])
                self._abort_if_unique_id_configured()
                self._data = {CONF_API_TOKEN: user_input[CONF_API_TOKEN], CONF_API_BASE: base}
                return await self.async_step_entities()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_API_BASE, default=DEFAULT_API_BASE): str,
                }
            ),
            errors=errors,
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: pick the sensors + options."""
        if user_input is not None:
            return self.async_create_entry(
                title="Kilonaut", data={**self._data, **user_input}
            )
        return self.async_show_form(step_id="entities", data_schema=_entities_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KilonautOptionsFlow:
        return KilonautOptionsFlow()


class KilonautOptionsFlow(OptionsFlow):
    """Let the user re-pick sensors / tweak the threshold later."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_entities_schema(current))
