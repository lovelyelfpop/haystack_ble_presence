"""Parse accessory battery status from BLE manufacturer data.

Mirrors the Flutter logic:

    final md = r.advertisementData.manufacturerData[0x4C];
    if (md != null) {
      int status = md[2];
      switch ((status >> 6) & 0x3) { ... }
    }

Note: the `withMsd` mask filter in the Flutter code is a red herring.
FlutterBluePlus combines scan filters with OR semantics and the Dart code
never re-checks the mask, so the battery is simply the top two bits of the
third byte of the Apple (0x4C) manufacturer data.

Level mapping:
    0 -> ok, 1 -> medium, 2 -> low, 3 -> critical
"""

from __future__ import annotations

from collections.abc import Mapping

APPLE_MANUFACTURER_ID = 0x4C

BATTERY_LEVELS: tuple[str, ...] = ("ok", "medium", "low", "critical")


def parse_battery_level(
    manufacturer_data: Mapping[int, bytes] | None,
) -> str | None:
    """Return 'ok'/'medium'/'low'/'critical', or None if not applicable."""
    if not manufacturer_data:
        return None
    payload = manufacturer_data.get(APPLE_MANUFACTURER_ID)
    if not payload or len(payload) < 3:
        return None
    # Some stacks keep the little-endian company id at the front; strip it.
    if len(payload) >= 5 and payload[0] == 0x4C and payload[1] == 0x00:
        payload = payload[2:]
    return BATTERY_LEVELS[(payload[2] >> 6) & 0x03]
