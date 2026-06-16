"""Bridge `connect` RPC must be able to RESTART a dead cloud poller.

Regression for 2026-06-16: the monitor's shutdown sends `disconnect`
(cancels cloud_task) and /api/reconnect does disconnect→connect. The
`connect` handler used to only set force_repoll, which is a no-op once
the task is cancelled — so the cloud session stayed dead after every
monitor restart until the bridge process itself was restarted.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


def _fresh_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("JACKERY_CREDS_FILE", str(tmp_path / "jackery-creds.json"))
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE", str(tmp_path / ".key"))
    import crypto_util
    importlib.reload(crypto_util)
    import bridge
    importlib.reload(bridge)
    # State() built its Event at import time; rebind to the running loop.
    bridge.state.cloud_force_repoll = asyncio.Event()
    return bridge


async def _stub_loop():
    await asyncio.sleep(3600)  # stand-in for the real cloud poller


@pytest.mark.asyncio
async def test_connect_restarts_dead_cloud_task(tmp_path, monkeypatch):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "cloud_loop", _stub_loop)
    bridge.state.cloud_creds = {"email": "u@example.com",
                                "password": "pw", "region": "US"}

    # Simulate a cancelled/finished cloud_task (post-disconnect).
    async def _noop():
        return
    dead = asyncio.create_task(_noop())
    await dead
    bridge.state.cloud_task = dead
    assert dead.done()

    await bridge.handle("connect", {})

    # connect must have created a NEW, live cloud_task.
    assert bridge.state.cloud_task is not None
    assert bridge.state.cloud_task is not dead
    assert not bridge.state.cloud_task.done()
    bridge.state.cloud_task.cancel()


@pytest.mark.asyncio
async def test_connect_nudges_live_cloud_task(tmp_path, monkeypatch):
    """When the poller is alive, connect must NOT spawn a second loop —
    it just nudges via force_repoll."""
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "cloud_loop", _stub_loop)
    bridge.state.cloud_creds = {"email": "u@example.com",
                                "password": "pw", "region": "US"}

    live = asyncio.create_task(_stub_loop())
    bridge.state.cloud_task = live
    bridge.state.cloud_force_repoll.clear()

    await bridge.handle("connect", {})

    assert bridge.state.cloud_task is live          # not replaced
    assert bridge.state.cloud_force_repoll.is_set()  # nudged
    live.cancel()


@pytest.mark.asyncio
async def test_connect_without_creds_does_not_spawn(tmp_path, monkeypatch):
    """No creds → don't spawn a loop that would just exit immediately;
    fall back to the nudge path."""
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "cloud_loop", _stub_loop)
    bridge.state.cloud_creds = None
    bridge.state.cloud_task = None

    await bridge.handle("connect", {})

    assert bridge.state.cloud_task is None  # nothing spawned
