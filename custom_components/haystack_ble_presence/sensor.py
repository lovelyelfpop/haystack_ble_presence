"""Sensor platform: last seen proxy / rssi / time for each tracked device."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from .adv_parser import APPLE_MANUFACTURER_ID, parse_battery_level
from .const import DOMAIN, device_identifier, unique_id_for
from .hub import BlePresenceHub, TrackedDevice


class _DeviceSensor(SensorEntity):
    """Base sensor that reads live state from the hub."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice, kind: str) -> None:
        self._hub = hub
        self._device_id = device.device_id
        self._attr_unique_id = unique_id_for(device.device_id, kind)
        self._attr_device_info = dr.DeviceInfo(
            identifiers={device_identifier(device.device_id)},
            name=device.device_id,
            manufacturer="BLE",
            model="Key-derived tracker",
        )

    def _device(self) -> TrackedDevice | None:
        return self._hub.get_device(self._device_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._hub.entity_added(self, self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._hub.entity_removed(self, self._device_id)


class BleLastSeenSensor(_DeviceSensor):
    """When this device was last heard from."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice) -> None:
        super().__init__(hub, device, "last_seen")
        self._attr_translation_key = "last_seen"

    @property
    def native_value(self) -> object:
        device = self._device()
        return device.last_seen_dt if device is not None else None


class BleRssiSensor(_DeviceSensor):
    """RSSI of the last matching advertisement."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "dBm"

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice) -> None:
        super().__init__(hub, device, "rssi")
        self._attr_translation_key = "rssi"

    @property
    def native_value(self) -> object:
        device = self._device()
        return device.last_rssi if device is not None else None


class BleProxySensor(_DeviceSensor):
    """Name of the BLE proxy that received the last matching advertisement."""

    _attr_icon = "mdi:bluetooth-transfer"

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice) -> None:
        super().__init__(hub, device, "proxy")
        self._attr_translation_key = "proxy"

    @property
    def native_value(self) -> object:
        device = self._device()
        if device is None or not device.last_source:
            return None
        return self._hub.proxy_name(device.last_source)


class BleProxyAreaSensor(_DeviceSensor):
    """Home Assistant area in which the receiving BLE proxy is located."""

    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice) -> None:
        super().__init__(hub, device, "proxy_area")
        self._attr_translation_key = "proxy_area"

    @property
    def native_value(self) -> object:
        device = self._device()
        if device is None:
            return None
        return self._hub.proxy_area(device.last_source)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        device = self._device()
        if device is None:
            return {}
        return {"source": device.last_source}


class BleBatterySensor(_DeviceSensor):
    """Battery level reported in the accessory advertisement."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "medium", "low", "critical"]

    def __init__(self, hub: BlePresenceHub, device: TrackedDevice) -> None:
        super().__init__(hub, device, "battery")
        self._attr_translation_key = "battery"

    @property
    def native_value(self) -> object:
        device = self._device()
        return device.battery if device is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        device = self._device()
        if device is None:
            return {}
        samples = device.manufacturer_samples or []
        parsed = {
            sample: parse_battery_level(
                {APPLE_MANUFACTURER_ID: bytes.fromhex(sample)}
            )
            for sample in samples
        }
        return {
            "manufacturer_data": device.manufacturer_data,
            "manufacturer_samples": samples,
            "parsed_samples": parsed,
        }

    @property
    def icon(self) -> str:
        device = self._device()
        level = device.battery if device is not None else None
        return {
            "ok": "mdi:battery",
            "medium": "mdi:battery-50",
            "low": "mdi:battery-20",
            "critical": "mdi:battery-alert",
        }.get(level, "mdi:battery-unknown")


def build_entities(hub: BlePresenceHub, device: TrackedDevice) -> Sequence[Entity]:
    return [
        BleLastSeenSensor(hub, device),
        BleRssiSensor(hub, device),
        BleProxySensor(hub, device),
        BleProxyAreaSensor(hub, device),
        BleBatterySensor(hub, device),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[Sequence[Entity], bool], None],
) -> bool:
    """Set up sensors from a config entry."""
    hub: BlePresenceHub = hass.data[DOMAIN][entry.entry_id]

    def _builder(device: TrackedDevice) -> Sequence[Entity]:
        return build_entities(hub, device)

    hub.register_entity_platform("sensor", _builder, async_add_entities)
    return True
