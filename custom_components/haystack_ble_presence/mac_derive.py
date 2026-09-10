"""Derive BLE advertisement MAC addresses from SECP224R1 private keys.

This mirrors the algorithm from test.py:
  raw private key (28 bytes) -> private scalar -> public point on secp224r1
  -> take the first 6 bytes of the X coordinate -> force the two top bits
  of the first byte (0xC0) -> use as the broadcast MAC address.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable

__all__ = [
    "cryptography_available",
    "derive_all",
    "derive_mac",
    "normalize_mac",
]

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import ec

    _curve = ec.SECP224R1()
    _backend = default_backend()
except Exception:  # pragma: no cover - cryptography is bundled with HA
    _curve = None
    _backend = None


def cryptography_available() -> bool:
    """Return True when the cryptography backend could be imported."""
    return _curve is not None and _backend is not None


def derive_mac(b64_private_key: str) -> str:
    """Derive a single MAC address string ('XX:XX:...') from a base64 key.

    Raises ValueError/TypeError when the key cannot be decoded or the scalar
    is not a valid secp224r1 private key.
    """
    if _curve is None:
        raise RuntimeError("cryptography library is not available")

    raw = base64.b64decode(b64_private_key.strip(), validate=True)
    if not raw:
        raise ValueError("empty key material")

    priv_int = int.from_bytes(raw, byteorder="big")
    priv = ec.derive_private_key(priv_int, _curve, _backend)
    x_int = priv.public_key().public_numbers().x
    x_bytes = x_int.to_bytes(28, byteorder="big")

    mac = bytearray(x_bytes[:6])
    mac[0] |= 0xC0
    return ":".join(f"{b:02X}" for b in mac)


def derive_all(base64_keys: Iterable[str]) -> set[str]:
    """Derive MACs for many keys, silently skipping keys that fail."""
    macs: set[str] = set()
    for key in base64_keys:
        if not key:
            continue
        try:
            macs.add(derive_mac(key))
        except (ValueError, TypeError, binascii.Error):
            continue
    return macs


def normalize_mac(address: str) -> str:
    """Normalize an address to 'XX:XX:XX:XX:XX:XX' (uppercase)."""
    cleaned = address.replace("-", ":").replace("_", ":").upper()
    if len(cleaned) == 12 and ":" not in cleaned:
        cleaned = ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    return cleaned
