"""BLE key-derived MAC presence integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS, SERVICE_RESCAN
from .hub import BlePresenceHub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    others = [
        other.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if others:
        _LOGGER.warning(
            "Multiple '%s' config entries are configured (%s). Entries that "
            "read the same key files create the same device ids, so their "
            "entities conflict ('does not generate unique IDs'). Please keep "
            "only one entry, or point them at different key files.",
            DOMAIN,
            ", ".join(others),
        )

    hub = BlePresenceHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hub.async_initial_load()
    await hub.async_prune_orphan_entities()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    async def _handle_rescan(_call) -> None:
        await hub.async_rescan()

    if not hass.services.has_service(DOMAIN, SERVICE_RESCAN):
        hass.services.async_register(DOMAIN, SERVICE_RESCAN, _handle_rescan)

    hub.remove_update_listener = entry.add_update_listener(_async_update_listener)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hub: BlePresenceHub | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is not None:
        hub.async_stop()
        if callable(hub.remove_update_listener):
            hub.remove_update_listener()
        hub.remove_update_listener = None

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN) and hass.services.has_service(
            DOMAIN, SERVICE_RESCAN
        ):
            hass.services.async_remove(DOMAIN, SERVICE_RESCAN)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to an options change."""
    hub: BlePresenceHub | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is None:
        return
    hub.update_config()
    await hub.async_rescan()
