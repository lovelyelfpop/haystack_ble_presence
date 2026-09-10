"""Constants for the BLE key-derived MAC presence integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.util import slugify

DOMAIN = "haystack_ble_presence"
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Configuration keys
CONF_DIRS = "dirs"
CONF_OFFLINE_TIMEOUT = "offline_timeout"

# Default directory (relative to the HA config dir) that holds the key files
DEFAULT_KEY_DIR = "haystack_keys"

DEFAULT_OFFLINE_TIMEOUT = 30
MIN_OFFLINE_TIMEOUT = 10

# State / service names
SERVICE_RESCAN = "rescan"

# Attribute keys used on entities
ATTR_LAST_SEEN = "last_seen"
ATTR_SOURCE = "source"
ATTR_RSSI = "rssi"
ATTR_BATTERY = "battery"
ATTR_RECEIVERS = "receivers"
ATTR_KEY_COUNT = "key_count"
ATTR_DERIVED_COUNT = "derived_count"


def unique_id_for(device_id: str, kind: str) -> str:
    """Return a stable unique id for an entity of a tracked device."""
    return f"{DOMAIN}_{slugify(device_id)}_{kind}"


def device_identifier(device_id: str) -> tuple[str, str]:
    """Return the device registry identifier tuple used for a tracked device."""
    return (DOMAIN, slugify(device_id))
