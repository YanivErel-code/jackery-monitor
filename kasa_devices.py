"""
Persisted registry of Kasa devices the user has tested and saved.

Rules in automation.py reference devices by `host`, so deleting a saved
device that's still referenced is allowed but warns the caller — the rule
will start failing on next evaluation, which is visible in the Logs tab.

Storage: /data/kasa_devices.json. Format:
{
  "devices": [
    {"host": "192.168.1.50", "alias": "Heater", "model": "KP125",
     "type": "PLUG", "added": <unix-ts>, "last_tested": <unix-ts>}
  ]
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

log = logging.getLogger("kasa_devices")

DEVICES_PATH = os.environ.get("JACKERY_KASA_DEVICES_FILE", "/data/kasa_devices.json")


class KasaRegistry:
    def __init__(self) -> None:
        self.devices: list[dict] = []
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(DEVICES_PATH) as f:
                data = json.load(f)
            self.devices = list(data.get("devices") or [])
        except FileNotFoundError:
            self.devices = []
        except Exception as e:
            log.warning("kasa devices file %s unreadable: %s; starting empty",
                        DEVICES_PATH, e)
            self.devices = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(DEVICES_PATH) or ".", exist_ok=True)
            tmp = DEVICES_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"devices": self.devices}, f, indent=2)
            os.replace(tmp, DEVICES_PATH)
        except Exception as e:
            log.error("failed to save kasa devices: %s", e)

    def list_devices(self, jackery_device_sn: str | None = None) -> list[dict]:
        """All saved Kasa devices, optionally filtered to those assigned
        to a specific Jackery. Devices with no assignment ("unassigned"
        legacy entries) are always returned regardless of filter, so a
        user with pre-assignment data isn't suddenly locked out of their
        own plugs."""
        if jackery_device_sn is None:
            return list(self.devices)
        return [
            d for d in self.devices
            if not d.get("jackery_device_sn") or
               d.get("jackery_device_sn") == jackery_device_sn
        ]

    def get(self, host: str) -> dict | None:
        return next((d for d in self.devices if d.get("host") == host), None)

    def upsert(self, host: str, alias: str = "", model: str | None = None,
               type_: str | None = None, mark_tested: bool = False,
               jackery_device_sn: str | None = None) -> dict:
        host = (host or "").strip()
        if not host:
            raise ValueError("host is required")
        alias = (alias or "").strip() or host
        now = time.time()
        # Empty string means "explicitly unassign". None means "leave as-is"
        # so a partial save (e.g. just renaming) doesn't accidentally drop
        # the existing assignment.
        normalised_assignment = (
            None if jackery_device_sn is None
            else (jackery_device_sn.strip() or None)
        )
        existing = self.get(host)
        if existing:
            existing["alias"] = alias
            if model is not None:
                existing["model"] = model
            if type_ is not None:
                existing["type"] = type_
            if mark_tested:
                existing["last_tested"] = now
            if jackery_device_sn is not None:
                existing["jackery_device_sn"] = normalised_assignment
        else:
            existing = {
                "host":  host,
                "alias": alias,
                "model": model,
                "type":  type_,
                "added": now,
                "last_tested": now if mark_tested else None,
                "jackery_device_sn": normalised_assignment,
            }
            self.devices.append(existing)
        self._save()
        return existing

    def delete(self, host: str) -> bool:
        before = len(self.devices)
        self.devices = [d for d in self.devices if d.get("host") != host]
        changed = len(self.devices) != before
        if changed:
            self._save()
        return changed

    def hosts_in_use(self, rules: list[dict]) -> set[str]:
        """Return the set of saved-device hosts referenced by any rule."""
        return {r.get("kasa_host") for r in (rules or []) if r.get("kasa_host")}
