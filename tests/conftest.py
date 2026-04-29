"""
Pytest fixtures shared across the unit tests.

Each test gets a fresh /tmp directory mapped into the env vars our modules
use to locate their on-disk state, so tests don't bleed into each other or
into `/data` on the dev machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo root importable so tests can `import auth`, `import settings`
# etc. without a setup.py / src layout.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def isolated_data(tmp_path, monkeypatch):
    """Redirect every module's persistent-state file under /data to tmp_path
       so tests don't touch the dev box's /data and don't pollute each other.

       Returns the tmp_path so individual tests can probe what was written."""
    data = tmp_path
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE",     str(data / ".key"))
    monkeypatch.setenv("JACKERY_CREDS_FILE",           str(data / "jackery-creds.json"))
    monkeypatch.setenv("JACKERY_KASA_CREDS_FILE",      str(data / "kasa-creds.json"))
    monkeypatch.setenv("JACKERY_KASA_DEVICES_FILE",    str(data / "kasa_devices.json"))
    monkeypatch.setenv("JACKERY_RULES_FILE",           str(data / "automation.json"))
    monkeypatch.setenv("JACKERY_SETTINGS_FILE",        str(data / "settings.json"))
    monkeypatch.setenv("JACKERY_AUTH_FILE",            str(data / "auth.json"))
    monkeypatch.setenv("JACKERY_LOCATION_FILE",        str(data / "location.json"))
    monkeypatch.setenv("JACKERY_COST_FILE",            str(data / "cost.json"))
    monkeypatch.setenv("JACKERY_SMART_CHARGE_FILE",    str(data / "smart_charge.json"))
    monkeypatch.setenv("JACKERY_CLAUDE_KEY_FILE",      str(data / ".claude-key"))
    # Force module-level path constants to re-read the env vars by reloading.
    # Tests that import these modules import them inside the test function
    # AFTER this fixture runs so the constants pick up the patched paths.
    return data
