"""Load and parse the device JSON files that carry the BLE keys.

A device file is expected to look like ./1.json. To be tolerant we accept a
file that contains a single device object or a (JSON) list of device objects.

Each device object can use these fields:
    id / name            - human readable identifier (id is preferred)
    privateKey           - base64 secp224r1 private key
    additionalKeys       - list of extra base64 keys
    ... (other fields are ignored)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .mac_derive import derive_all

_LOGGER = logging.getLogger(__name__)

__all__ = ["DeviceRecord", "load_device_files"]


@dataclass(slots=True)
class DeviceRecord:
    """A single trackable device plus all of its broadcast MAC addresses."""

    device_id: str
    name: str
    macs: frozenset[str] = field(default_factory=frozenset)
    source_file: str = ""
    key_count: int = 0
    derived_count: int = 0


def _record_from_payload(payload: object, source: str) -> DeviceRecord | None:
    if not isinstance(payload, dict):
        return None

    device_id = str(payload.get("id") or payload.get("name") or "").strip()
    if not device_id:
        return None

    name = str(payload.get("name") or device_id).strip() or device_id

    keys: list[str] = []
    private = payload.get("privateKey")
    if isinstance(private, str) and private:
        keys.append(private)
    extra = payload.get("additionalKeys")
    if isinstance(extra, list):
        keys.extend(str(k) for k in extra if isinstance(k, str) and k.strip())

    if not keys:
        _LOGGER.warning(
            "Device '%s' in %s has no usable keys", device_id, source
        )
        return DeviceRecord(
            device_id=device_id, name=name, source_file=source, key_count=0
        )

    derived = derive_all(keys)
    if not derived:
        _LOGGER.warning(
            "None of the %d key(s) for device '%s' in %s could be converted "
            "into a MAC address",
            len(keys),
            device_id,
            source,
        )
    return DeviceRecord(
        device_id=device_id,
        name=name,
        macs=frozenset(derived),
        source_file=source,
        key_count=len(keys),
        derived_count=len(derived),
    )


def _records_from_document(document: object, source: str) -> list[DeviceRecord]:
    if isinstance(document, list):
        items: list[object] = document
    else:
        items = [document]

    records: list[DeviceRecord] = []
    for index, payload in enumerate(items):
        record = _record_from_payload(payload, source)
        if record is not None:
            records.append(record)
    return records


def load_device_files(dirs: list[str]) -> list[DeviceRecord]:
    """Scan directories for *.json files and return every device record.

    This function is blocking (JSON parsing + EC math) so call it from an
    executor when running inside Home Assistant.
    """
    records: list[DeviceRecord] = []
    seen_files: set[str] = set()

    for raw_dir in dirs:
        root = Path(str(raw_dir).strip().strip('"').strip("'"))
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not create key directory %s: %s", root, err)
            continue
        if not root.is_dir():
            _LOGGER.warning("BLE key path is not a directory: %s", root)
            continue

        for path in sorted(root.glob("*.json")):
            key = str(path)
            if key in seen_files:
                continue
            seen_files.add(key)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as err:
                _LOGGER.warning("Could not read %s: %s", path, err)
                continue
            try:
                document = json.loads(text)
            except json.JSONDecodeError as err:
                _LOGGER.warning("Invalid JSON in %s: %s", path, err)
                continue
            records.extend(_records_from_document(document, key))

    # De-duplicate records that share a device id, keeping the one that
    # produced the most usable MAC addresses.
    by_id: dict[str, DeviceRecord] = {}
    for record in records:
        existing = by_id.get(record.device_id)
        if existing is None or len(record.macs) > len(existing.macs):
            by_id[record.device_id] = record
    return list(by_id.values())
