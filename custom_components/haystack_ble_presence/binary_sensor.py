"""Binary sensor platform: presence (home/away) per tracked device."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, device_identifier, unique_id_for
from .hub import BlePresenceHub, TrackedDevice, device_extra_attributes


class BlePresenceBinarySensor(BinarySensorEntity):
    """A single 'is the device at home' binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_has_entity_name = True
    _attr_translation_key = "presence"
    _attr_should_poll = False

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice) -> None:
        self._hub = hub
        self._device_id = device.device_id
        self._attr_unique_id = unique_id_for(device.device_id, "presence")
        self._attr_device_info = dr.DeviceInfo(
            identifiers={device_identifier(device.device_id)},
            name=device.device_id,
            manufacturer="BLE",
            model="Key-derived tracker",
        )

    @property
    def icon(self) -> str:
        device = self._device()
        return (
            "mdi:home-account"
            if device is not None and device.home
            else "mdi:home-export-outline"
        )

    @property
    def is_on(self) -> bool | None:
        device = self._device()
        return device.home if device is not None else False

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        device = self._device()
        return device_extra_attributes(device) if device is not None else {}

    def _device(self) -> TrackedDevice | None:
        return self._hub.get_device(self._device_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._hub.entity_added(self, self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._hub.entity_removed(self, self._device_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[Sequence[Entity], bool], None],
) -> bool:
    """Set up binary sensors from a config entry."""
    hub: BlePresenceHub = hass.data[DOMAIN][entry.entry_id]

    def _builder(device: TrackedDevice) -> Sequence[Entity]:
        return [BlePresenceBinarySensor(hub, device)]

    hub.register_entity_platform("binary_sensor", _builder, async_add_entities)
    return True
