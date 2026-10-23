"""Constants for the Kilonaut integration."""

from __future__ import annotations

DOMAIN = "kilonaut"

DEFAULT_API_BASE = "https://kilonaut.de/api/v1"

# Config keys
CONF_API_TOKEN = "api_token"
CONF_API_BASE = "api_base"
CONF_POWER_ENTITY = "power_entity"
CONF_ENERGY_ENTITY = "energy_entity"
CONF_ODOMETER_ENTITY = "odometer_entity"
CONF_THRESHOLD_W = "charging_threshold_w"
CONF_LOCATION_TYPE = "location_type"

# Defaults
DEFAULT_THRESHOLD_W = 1400
DEFAULT_LOCATION_TYPE = "home"

# Behaviour
HEARTBEAT_INTERVAL_S = 120  # live "charging now" update cadence
ODOMETER_INTERVAL_H = 24    # how often a configured odometer is pushed
MIN_SESSION_KWH = 0.1       # ignore charges smaller than this
RETRY_INTERVAL_MIN = 5      # how often undelivered charges are sent again

LOCATION_TYPES = ["home", "public", "work", "other"]
