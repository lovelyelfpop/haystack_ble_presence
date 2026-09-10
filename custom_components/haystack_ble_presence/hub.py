"""Core hub: loads device key files and tracks BLE advertisement presence."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity

from .adv_parser import parse_battery_level
from .const import (
    ATTR_BATTERY,
    ATTR_DERIVED_COUNT,
    ATTR_KEY_COUNT,
    ATTR_LAST_SEEN,
    ATTR_RECEIVERS,
    ATTR_RSSI,
    ATTR_SOURCE,
    CONF_DIRS,
    CONF_OFFLINE_TIMEOUT,
    DEFAULT_OFFLINE_TIMEOUT,
    device_identifier,
    unique_id_for,
)
from .devices import DeviceRecord, load_device_files
from .mac_derive import cryptography_available, normalize_mac

if TYPE_CHECKING:
    from homeassistant.core import ServiceCall

_LOGGER = logging.getLogger(__name__)

# Minimum amount of seconds between two state pushes for the same device while
# it is continuously seen (keeps the event rate low for busy environments).
_PUSH_THROTTLE_SECONDS = 5.0

# How long a resolved proxy->area mapping is reused before re-reading the
# device/area registries.
_PROXY_AREA_CACHE_SECONDS = 30.0

# Periodic active-scan window (some accessories put battery in scan responses).
_ACTIVE_SCAN_INTERVAL = 300.0
_ACTIVE_SCAN_DURATION = 10.0

# Entity kinds created per tracked device (must match the platform builders).
_ENTITY_KINDS = ("presence", "last_seen", "rssi", "proxy", "proxy_area", "battery")

# Device registry connection type used for bluetooth addresses.
_CONNECTION_BLUETOOTH = "bluetooth"


@dataclass
class ReceiverStat:
    """Statistics about advertisements received from one proxy."""

    source: str
    count: int = 0
    last_seen_ts: float = 0.0
    last_seen_dt: datetime | None = None
    last_rssi: float | None = None


@dataclass
class TrackedDevice:
    """Live presence state of one tracked device."""

    device_id: str
    name: str
    macs: frozenset[str] = field(default_factory=frozenset)
    key_count: int = 0
    derived_count: int = 0
    home: bool = False
    last_seen_dt: datetime | None = None
    last_seen_ts: float = 0.0
    last_source: str | None = None
    last_rssi: float | None = None
    battery: str | None = None
    manufacturer_data: dict[str, str] | None = None
    manufacturer_samples: list[str] = field(default_factory=list)
    receivers: dict[str, ReceiverStat] = field(default_factory=dict)
    _last_push_ts: float = 0.0
    _battery_logged: bool = False

    def record_seen(
        self,
        source: str | None,
        rssi: float | None,
        battery: str | None = None,
        manufacturer_data: Mapping[int, bytes] | None = None,
    ) -> bool:
        """Record a matching advertisement and return True if newly home."""
        was_home = self.home
        self.home = True
        now_ts = time.time()
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        self.last_seen_ts = now_ts
        self.last_seen_dt = now_dt
        self.last_rssi = rssi
        if source:
            self.last_source = source
        if battery is not None:
            self.battery = battery
            if not self._battery_logged:
                self._battery_logged = True
                _LOGGER.info(
                    "Device '%s' battery parsed: %s", self.device_id, battery
                )
        if manufacturer_data:
            self.manufacturer_data = {
                str(key): bytes(value).hex().upper()
                for key, value in manufacturer_data.items()
            }
            apple_payload = manufacturer_data.get(0x4C)
            if apple_payload:
                hex_sample = bytes(apple_payload).hex().upper()
                if (
                    hex_sample not in self.manufacturer_samples
                    and len(self.manufacturer_samples) < 20
                ):
                    self.manufacturer_samples.append(hex_sample)
        stat = self.receivers.get(source or "")
        if stat is None:
            stat = ReceiverStat(source=source or "unknown")
            self.receivers[source or ""] = stat
        stat.count += 1
        stat.last_seen_ts = now_ts
        stat.last_seen_dt = now_dt
        stat.last_rssi = rssi
        return was_home

    def should_push(self) -> bool:
        """Return True when enough time has passed to push a state update."""
        now = time.monotonic()
        if now - self._last_push_ts >= _PUSH_THROTTLE_SECONDS:
            self._last_push_ts = now
            return True
        return False


class BlePresenceHub:
    """Owns the device index, subscribes to BLE and keeps entities fresh."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.dirs = self._entry_dirs()
        self.offline_timeout = self._entry_option(
            CONF_OFFLINE_TIMEOUT, DEFAULT_OFFLINE_TIMEOUT
        )

        self._builders: dict[
            str,
            tuple[
                Callable[[TrackedDevice], Sequence[Entity]],
                Callable[[Sequence[Entity], bool], None],
            ],
        ] = {}
        self._registered_platforms: set[str] = set()
        self._devices: dict[str, TrackedDevice] = {}
        self._by_mac: dict[str, str] = {}
        self._entities: dict[str, list[Entity]] = {}

        self._remove_callbacks: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._closed = False
        self._rescan_lock = asyncio.Lock()
        self._proxy_area_cache: dict[str, tuple[float, str | None]] = {}
        self._proxy_name_cache: dict[str, tuple[float, str | None]] = {}
        self._subscribe_warned = False
        self._last_active_scan = 0.0
        self.remove_update_listener: Callable[[], None] | None = None

    # ------------------------------------------------------------------ #
    # configuration helpers
    # ------------------------------------------------------------------ #
    def _entry_dirs(self) -> list[str]:
        dirs = self.entry.options.get(CONF_DIRS) or self.entry.data.get(CONF_DIRS, [])
        return [str(d) for d in dirs]

    def _entry_option(self, key: str, default: int) -> int:
        return int(self.entry.options.get(key, self.entry.data.get(key, default)))

    def update_config(self) -> None:
        """Apply changed option values (called by the update listener)."""
        self.offline_timeout = self._entry_option(
            CONF_OFFLINE_TIMEOUT, DEFAULT_OFFLINE_TIMEOUT
        )
        self.dirs = self._entry_dirs()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def async_initial_load(self) -> None:
        """Blocking-free initial population of devices from disk."""
        await self.async_rescan()

    async def async_start(self) -> None:
        """Subscribe to BLE and start the maintenance loop."""
        if self._closed:
            return
        self._try_subscribe()
        self._task = self.hass.async_create_task(self._background_loop())

        # Make sure every entity is grouped under its device even when stale
        # (flat) entity registry rows exist from earlier versions.
        async def _reconcile() -> None:
            await asyncio.sleep(1)
            if self._closed:
                return
            try:
                await self.async_ensure_device_links()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("Device link reconciliation failed", exc_info=True)

        self.hass.async_create_task(_reconcile())

    def async_stop(self) -> None:
        """Unsubscribe and cancel the background loop."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for remove_callback in self._remove_callbacks:
            try:
                remove_callback()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to remove BLE callback", exc_info=True)
        self._remove_callbacks.clear()

    async def _background_loop(self) -> None:
        """Keep presence state fresh; device key files are only read once at
        startup/reload (or via an explicit options change / rescan service)."""
        while not self._closed:
            try:
                await asyncio.sleep(self._tick_seconds())
            except asyncio.CancelledError:
                break
            if self._closed:
                break
            try:
                self._try_subscribe()
                self._probe_devices()
                self._expire_devices()
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.exception("Unexpected error in background loop")

    def _tick_seconds(self) -> float:
        return max(2.0, min(float(self.offline_timeout) / 10.0, 10.0))

    # ------------------------------------------------------------------ #
    # public accessors
    # ------------------------------------------------------------------ #
    def get_device(self, device_id: str) -> TrackedDevice | None:
        return self._devices.get(device_id)

    @property
    def devices(self) -> dict[str, TrackedDevice]:
        return self._devices

    # ------------------------------------------------------------------ #
    # proxy -> area resolution
    # ------------------------------------------------------------------ #
    def proxy_area(self, source: str | None) -> str | None:
        """Return the Home Assistant area of the proxy for ``source``.

        ``source`` is the value carried by the BLE service info (for ESPHome
        proxies this is the proxy's MAC address). The result is cached for a
        short while because the device/area registries only change rarely.
        """
        if not source:
            return None
        now = time.monotonic()
        cached = self._proxy_area_cache.get(source)
        if cached is not None and now - cached[0] < _PROXY_AREA_CACHE_SECONDS:
            return cached[1]

        area = self._resolve_proxy_area(source)
        if len(self._proxy_area_cache) > 128:
            self._proxy_area_cache.clear()
        self._proxy_area_cache[source] = (now, area)
        return area

    def _resolve_proxy_area(self, source: str) -> str | None:
        try:
            from homeassistant.helpers.area_registry import async_get as area_get
            from homeassistant.helpers.device_registry import async_get as device_get
        except Exception:  # pragma: no cover - defensive
            return None

        target = _registry_key(source)
        try:
            registry = device_get(self.hass)
            devices = _iter_device_entries(registry)
            areas = area_get(self.hass)
        except Exception:  # pragma: no cover - defensive
            return None

        for device in devices:
            if getattr(device, "disabled_by", None):
                continue
            if not _device_matches_proxy(device, source, target):
                continue
            if not device.area_id:
                continue
            try:
                area = areas.async_get_area(device.area_id)
            except Exception:  # pragma: no cover - defensive
                area = None
            if area is not None:
                return area.name

        # Some proxies expose a MAC in ``source`` that differs from the MAC
        # used as the device registry identifier, so fall back to matching the
        # friendly name reported by the scanner.
        scanner_name = self._scanner_name(source)
        if not scanner_name:
            return None
        for device in devices:
            if getattr(device, "disabled_by", None):
                continue
            if not device.area_id:
                continue
            name = getattr(device, "name_by_user", None) or getattr(
                device, "name", ""
            )
            if not name or str(name).lower() != scanner_name.lower():
                continue
            try:
                area = areas.async_get_area(device.area_id)
            except Exception:  # pragma: no cover - defensive
                area = None
            if area is not None:
                return area.name
        return None

    def _scanner_name(self, source: str) -> str | None:
        """Return the friendly name HA knows for a scanner source."""
        try:
            from homeassistant.components.bluetooth import async_scanner_by_source

            scanner = async_scanner_by_source(self.hass, source)
        except Exception:  # pragma: no cover - bluetooth API differences
            return None
        if scanner is None:
            return None
        return getattr(scanner, "name", None) or None

    def proxy_name(self, source: str | None) -> str | None:
        """Return the friendly device name of the proxy for ``source``.

        For ESPHome proxies the service info source is the proxy MAC, so the
        scanner/device registry name is shown instead of the raw MAC.
        """
        if not source:
            return None
        now = time.monotonic()
        cached = self._proxy_name_cache.get(source)
        if cached is not None and now - cached[0] < _PROXY_AREA_CACHE_SECONDS:
            return cached[1]

        name = self._scanner_name(source)
        if not name:
            name = self._device_name_for_source(source)
        if name is None:
            name = source
        if len(self._proxy_name_cache) > 128:
            self._proxy_name_cache.clear()
        self._proxy_name_cache[source] = (now, name)
        return name

    def _device_name_for_source(self, source: str) -> str | None:
        try:
            from homeassistant.helpers.device_registry import async_get as device_get

            target = _registry_key(source)
            for device in _iter_device_entries(device_get(self.hass)):
                if getattr(device, "disabled_by", None):
                    continue
                if _device_matches_proxy(device, source, target):
                    return (
                        getattr(device, "name_by_user", None)
                        or getattr(device, "name", None)
                        or None
                    )
        except Exception:  # pragma: no cover - defensive
            return None
        return None

    # ------------------------------------------------------------------ #
    # entity platform registration (called by each platform setup)
    # ------------------------------------------------------------------ #
    def register_entity_platform(
        self,
        platform: str,
        builder: Callable[[TrackedDevice], Sequence[Entity]],
        add_entities: Callable[[Sequence[Entity], bool], None],
    ) -> None:
        """Register how a platform creates entities, adding current devices."""
        if platform in self._registered_platforms:
            _LOGGER.debug("Platform %s already registered, skipping", platform)
            return
        self._registered_platforms.add(platform)
        self._builders[platform] = (builder, add_entities)
        self._add_device_entities(list(self._devices.values()))

    def _add_device_entities(self, devices: Sequence[TrackedDevice]) -> None:
        for builder, add_entities in self._builders.values():
            entities = [entity for device in devices for entity in builder(device)]
            if entities:
                add_entities(entities, False)

    def entity_added(self, entity: Entity, device_id: str) -> None:
        self._entities.setdefault(device_id, []).append(entity)

    def entity_removed(self, entity: Entity, device_id: str) -> None:
        entities = self._entities.get(device_id)
        if entities and entity in entities:
            entities.remove(entity)

    async def async_ensure_device_links(self) -> None:
        """Group every entity registry row of this entry under its device.

        This is run shortly after each setup/reload so entities that were
        created flat by earlier versions (or that predate the current entity
        registry layout) get linked to the device registry entry of their key
        file id (e.g. ``IG5LW8``).
        """
        if not self._devices:
            return
        try:
            from homeassistant.helpers.device_registry import async_get as device_get
            from homeassistant.helpers.entity_registry import async_get as entity_get

            entity_registry = entity_get(self.hass)
            device_registry = device_get(self.hass)
        except Exception:  # pragma: no cover - defensive
            return

        expected: dict[str, str] = {}
        for device in self._devices.values():
            for kind in _ENTITY_KINDS:
                expected[unique_id_for(device.device_id, kind)] = device.device_id

        for entry in list(entity_registry.entities.values()):
            device_id = expected.get(entry.unique_id)
            if device_id is None:
                continue
            device = self._devices.get(device_id)
            if device is None:
                continue
            device_entry = _create_device_entry(
                device_registry, self.entry.entry_id, device_id, device.device_id
            )
            if entry.device_id != device_entry.id:
                entity_registry.async_update_entity(
                    entry.entity_id, device_id=device_entry.id
                )

    async def async_prune_orphan_entities(self) -> None:
        """Remove stale entity registry rows whose config entry is gone.

        Older installs/reloads can leave orphaned registry rows pointing at a
        deleted config entry; they then block new entities with the same
        unique id ("already exists - ignoring").
        """
        if not self._devices:
            return
        try:
            from homeassistant.helpers.entity_registry import async_get as entity_get

            entity_registry = entity_get(self.hass)
        except Exception:  # pragma: no cover - defensive
            return

        expected = {
            unique_id_for(device.device_id, kind)
            for device in self._devices.values()
            for kind in _ENTITY_KINDS
        }
        for entry in list(entity_registry.entities.values()):
            if entry.unique_id not in expected:
                continue
            config_entry_id = entry.config_entry_id
            if config_entry_id is None:
                continue
            if self.hass.config_entries.async_get_entry(config_entry_id) is not None:
                continue
            try:
                entity_registry.async_remove(entry.entity_id)
                _LOGGER.info(
                    "Removed orphan entity registry entry %s (%s)",
                    entry.entity_id,
                    entry.unique_id,
                )
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug(
                    "Could not remove orphan entity %s", entry.entity_id, exc_info=True
                )

    # ------------------------------------------------------------------ #
    # bluetooth subscription
    # ------------------------------------------------------------------ #
    def _try_subscribe(self) -> None:
        if self._closed or self._remove_callbacks:
            return
        if not cryptography_available():
            return
        try:
            from homeassistant.components import bluetooth

            register = bluetooth.async_register_callback
            parameters = inspect.signature(register).parameters

            def _subscribe(match: dict[str, object]) -> Callable[[], None]:
                kwargs: dict[str, object] = {"match_dict": match}
                if "mode" in parameters:
                    from homeassistant.components.bluetooth import (
                        BluetoothScanningMode,
                    )

                    kwargs["mode"] = BluetoothScanningMode.ACTIVE
                elif "adapter" in parameters:
                    kwargs["adapter"] = None
                return register(self.hass, self._handle_service_info, **kwargs)

            # Register for both pipelines: non-connectable (rotating random
            # addresses) and connectable advertisements.
            callbacks: list[Callable[[], None]] = []
            errors: list[str] = []
            for match in ({"connectable": False}, {}):
                try:
                    callbacks.append(_subscribe(match))
                except Exception as err:  # try the other pipeline
                    errors.append(str(err))
            if not callbacks:
                raise RuntimeError("; ".join(errors) or "registration failed")
            self._remove_callbacks = callbacks
            self._subscribe_warned = False
            _LOGGER.info(
                "Subscribed to BLE advertisement callbacks (%d registration(s))",
                len(callbacks),
            )
        except Exception as err:  # bluetooth component not ready yet
            self._remove_callbacks = []
            if self._subscribe_warned:
                _LOGGER.debug("BLE subscription not available yet: %s", err)
            else:
                self._subscribe_warned = True
                _LOGGER.warning(
                    "Could not subscribe to BLE advertisements yet: %s. "
                    "Presence detection will keep polling until it succeeds.",
                    err,
                )

    def _handle_service_info(self, service_info: object, _change: object = None) -> None:
        """Called for every BLE advertisement received by any proxy."""
        address = getattr(service_info, "address", None)
        if not address:
            return
        mac = normalize_mac(address)
        device_id = self._by_mac.get(mac)
        if device_id is None:
            return
        device = self._devices.get(device_id)
        if device is None:
            return
        source = getattr(service_info, "source", None)
        rssi = getattr(service_info, "rssi", None)
        manufacturer_data = _manufacturer_data(service_info)
        battery = parse_battery_level(manufacturer_data)
        was_home = device.record_seen(source, rssi, battery, manufacturer_data)
        if not was_home or device.should_push():
            self._push_state(device)

    def _push_state(self, device: TrackedDevice) -> None:
        for entity in self._entities.get(device.device_id, []):
            try:
                entity.async_write_ha_state()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("State push failed for %s", entity.entity_id, exc_info=True)

    def _probe_devices(self) -> None:
        """Poll the bluetooth manager's discovered-device snapshot.

        Newer HA versions deduplicate identical advertisements, so relying on
        per-advertisement callbacks alone can miss a device that broadcasts
        the same payload on a stable address. Reading the manager's current
        discovered list is robust: as long as a proxy sees the device it will
        show up here on every tick.
        """
        try:
            from homeassistant.components.bluetooth import async_discovered_service_info
        except Exception:  # pragma: no cover - bluetooth not loaded yet
            return

        try:
            service_infos = list(async_discovered_service_info(self.hass, False))
            service_infos += list(async_discovered_service_info(self.hass, True))
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("BLE discovery snapshot not available yet", exc_info=True)
            return

        touched: set[str] = set()
        for service_info in service_infos:
            address = getattr(service_info, "address", None)
            if not address:
                continue
            mac = normalize_mac(address)
            device_id = self._by_mac.get(mac)
            if device_id is None or device_id in touched:
                continue
            device = self._devices.get(device_id)
            if device is None:
                continue
            touched.add(device_id)
            source = getattr(service_info, "source", None)
            rssi = getattr(service_info, "rssi", None)
            manufacturer_data = _manufacturer_data(service_info)
            battery = parse_battery_level(manufacturer_data)
            was_home = device.record_seen(source, rssi, battery, manufacturer_data)
            if not was_home or device.should_push():
                self._push_state(device)

        # Some accessories only expose extra data (e.g. battery) in a scan
        # response, which requires active scanning. Ask for a periodic window.
        now = time.monotonic()
        if now - self._last_active_scan >= _ACTIVE_SCAN_INTERVAL:
            self._last_active_scan = now
            self.hass.async_create_task(self._async_request_active_scan())

    async def _async_request_active_scan(self) -> None:
        try:
            from homeassistant.components.bluetooth import async_request_active_scan
        except Exception:  # older HA: no explicit active-scan API
            return
        try:
            await async_request_active_scan(self.hass, _ACTIVE_SCAN_DURATION)
            _LOGGER.debug("Requested a %ss active scan window", _ACTIVE_SCAN_DURATION)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("Active scan request failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # expiry
    # ------------------------------------------------------------------ #
    def _expire_devices(self) -> None:
        now = time.time()
        for device in self._devices.values():
            if device.home and now - device.last_seen_ts > self.offline_timeout:
                _LOGGER.info(
                    "Device '%s' (%s) not seen for %.0fs, marking away",
                    device.name,
                    device.device_id,
                    self.offline_timeout,
                )
                device.home = False
                device.last_source = None
                device.last_rssi = None
                self._push_state(device)

    # ------------------------------------------------------------------ #
    # device file (re)loading
    # ------------------------------------------------------------------ #
    async def async_rescan(self) -> None:
        """Re-read every configured JSON file and reconcile devices."""
        if self._rescan_lock.locked():
            return
        async with self._rescan_lock:
            records: list[DeviceRecord] = [
                record
                for record in await self.hass.async_add_executor_job(
                    load_device_files, self.dirs
                )
                if record.macs
            ]
            record_ids = {record.device_id for record in records}
            removed = [did for did in list(self._devices) if did not in record_ids]

            new_devices: list[TrackedDevice] = []
            for record in records:
                existing = self._devices.get(record.device_id)
                if existing is None:
                    device = TrackedDevice(
                        device_id=record.device_id,
                        name=record.name,
                        macs=record.macs,
                        key_count=record.key_count,
                        derived_count=record.derived_count,
                    )
                    self._devices[record.device_id] = device
                    new_devices.append(device)
                elif record.macs != existing.macs:
                    _LOGGER.info(
                        "MAC set for device '%s' changed (%d -> %d MACs)",
                        existing.name,
                        len(existing.macs),
                        len(record.macs),
                    )
                    existing.macs = record.macs
                    existing.key_count = record.key_count
                    existing.derived_count = record.derived_count

            self._rebuild_index()

            for device_id in removed:
                await self._remove_device(device_id)

            if new_devices:
                self._add_device_entities(new_devices)

            _LOGGER.info(
                "Loaded %d device(s) from %d key file(s); tracking %d MAC address(es)",
                len(self._devices),
                len(self.dirs),
                len(self._by_mac),
            )

    def _rebuild_index(self) -> None:
        self._by_mac.clear()
        for device in self._devices.values():
            for mac in device.macs:
                existing_id = self._by_mac.get(mac)
                if existing_id is not None and existing_id != device.device_id:
                    _LOGGER.warning(
                        "MAC %s is shared by devices '%s' and '%s'; keeping '%s'",
                        mac,
                        existing_id,
                        device.device_id,
                        existing_id,
                    )
                    continue
                self._by_mac[mac] = device.device_id

    async def _remove_device(self, device_id: str) -> None:
        device = self._devices.pop(device_id, None)
        if device is None:
            return
        self._rebuild_index()

        from homeassistant.helpers.device_registry import async_get as get_device_registry

        entities = list(self._entities.get(device_id, []))
        for entity in entities:
            try:
                await entity.async_remove()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("Removing entity %s failed", entity.entity_id, exc_info=True)
        self._entities.pop(device_id, None)

        try:
            registry = get_device_registry(self.hass)
            identifier = device_identifier(device_id)
            for device_entry in _iter_device_entries(registry):
                if identifier in getattr(device_entry, "identifiers", ()):
                    registry.async_remove_device(device_entry.id)
                    break
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("Removing device %s failed", device_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # service
    # ------------------------------------------------------------------ #
    async def async_service_rescan(self, call: ServiceCall | None = None) -> None:
        """Service handler that forces an immediate re-read of key files."""
        await self.async_rescan()


def _registry_key(value: str) -> str:
    """Normalize an address/id for registry comparison (aa:bb.. -> aabb..)."""
    return "".join(char for char in str(value).lower() if char.isalnum())


def _iter_device_entries(registry: object) -> list[object]:
    """Return device entries across HA versions.

    New HA returns an iterable of entries from ``registry.devices`` (mapping
    access is deprecated); old HA returns a dict keyed by device id.
    """
    devices = getattr(registry, "devices", None)
    if devices is None:
        return []
    entries: list[object] = []
    for item in devices:
        if isinstance(item, str):
            entry = devices[item]
            if entry is not None:
                entries.append(entry)
        else:
            entries.append(item)
    return entries


def _create_device_entry(
    registry: object, entry_id: str, device_id: str, name: str
) -> object:
    """Create/lookup a device registry entry across HA API versions.

    Older HA uses ``config_entries={...}``, newer HA requires the singular
    keyword-only ``config_entry_id``.
    """
    kwargs: dict[str, object] = {
        "identifiers": {device_identifier(device_id)},
        "name": name,
        "manufacturer": "BLE",
        "model": "Key-derived tracker",
    }
    parameters = inspect.signature(registry.async_get_or_create).parameters
    if "config_entry_id" in parameters:
        kwargs["config_entry_id"] = entry_id
    elif "config_entries" in parameters:
        kwargs["config_entries"] = {entry_id}
    return registry.async_get_or_create(**kwargs)


def _manufacturer_data(service_info: object) -> Mapping[int, bytes] | None:
    """Return manufacturer data from a service info, across HA versions."""
    data = getattr(service_info, "manufacturer_data", None)
    if data:
        return data
    advertisement = getattr(service_info, "advertisement", None)
    if advertisement is not None:
        data = getattr(advertisement, "manufacturer_data", None)
        if data:
            return data
    return None


def _device_matches_proxy(
    device: object, source: str, target: str
) -> bool:
    """Return True when a device registry entry represents the given proxy."""
    name = getattr(device, "name_by_user", None) or getattr(device, "name", "") or ""
    if name and str(name).lower() == source.lower():
        return True
    for conn_type, value in getattr(device, "connections", []):
        if conn_type == _CONNECTION_BLUETOOTH and _registry_key(value) == target:
            return True
    for _domain, value in getattr(device, "identifiers", []):
        if _registry_key(value) == target:
            return True
    return False


def device_extra_attributes(device: TrackedDevice) -> dict[str, object]:
    """Build the common attribute set shown on the entities."""
    receivers = [
        {
            "source": stat.source,
            "count": stat.count,
            "rssi": stat.last_rssi,
            "last_seen": stat.last_seen_dt.isoformat() if stat.last_seen_dt else None,
        }
        for stat in sorted(
            device.receivers.values(), key=lambda s: s.last_seen_ts, reverse=True
        )
    ]
    return {
        ATTR_LAST_SEEN: device.last_seen_dt.isoformat() if device.last_seen_dt else None,
        ATTR_SOURCE: device.last_source,
        ATTR_RSSI: device.last_rssi,
        ATTR_BATTERY: device.battery,
        ATTR_RECEIVERS: receivers,
        ATTR_KEY_COUNT: device.key_count,
        ATTR_DERIVED_COUNT: device.derived_count,
    }
