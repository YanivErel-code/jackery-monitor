"""Tests for backup.py — snapshot integrity, manifest checksums,
restore round-trip. The remote SMB mount is replaced by a local
directory (faux_mount) so these tests don't require a NAS or
CAP_SYS_ADMIN."""
from __future__ import annotations

import contextlib
import importlib
import json
import sqlite3

import pytest


@pytest.fixture()
def backup_env(tmp_path, monkeypatch):
    """Wire DATA_DIR + creds file at tmp paths and reload modules so
    their module-level constants pick up the new paths."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("JACKERY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JACKERY_BACKUP_CREDS_FILE",
                       str(tmp_path / "backup-creds.json"))
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE",
                       str(tmp_path / ".key"))

    import crypto_util
    importlib.reload(crypto_util)
    import backup_creds as bc
    importlib.reload(bc)
    import backup as bk
    importlib.reload(bk)

    # Save matching creds so backup.run_backup() / list_remote_snapshots()
    # don't bail with no_credentials.
    bc.save(host="nas.local", share="backups", username="u",
            password="p", subdir="jackery", domain="WORKGROUP")

    # Seed a tiny SQLite db + a couple of small files so the snapshot
    # has real content.
    db_path = data_dir / "energy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1),(2),(3);")
    conn.commit()
    conn.close()

    (data_dir / "settings.json").write_text(json.dumps({"hour": 3}))
    (data_dir / "auth.json").write_text(json.dumps({"user": "alice"}))

    return data_dir, tmp_path, bk, bc


def _patch_mount(monkeypatch, bk, fake_remote):
    """Replace mount_cifs with a context manager that yields a local
    directory standing in for the CIFS mountpoint."""
    @contextlib.contextmanager
    def fake(*_args, **_kwargs):
        fake_remote.mkdir(parents=True, exist_ok=True)
        yield fake_remote
    monkeypatch.setattr(bk, "mount_cifs", fake)


# -----------------------------------------------------------------------
# snapshot_db: online sqlite backup is byte-for-byte usable as a DB
# -----------------------------------------------------------------------

def test_snapshot_db_produces_a_valid_sqlite_file(backup_env, tmp_path):
    data_dir, _, bk, _ = backup_env
    src = data_dir / "energy.db"
    dst = tmp_path / "snap.db"
    size = bk.snapshot_db(src, dst)
    assert size > 0
    assert dst.exists()
    # Verify the snapshot is a real, queryable SQLite DB with our seed data.
    conn = sqlite3.connect(str(dst))
    rows = conn.execute("SELECT x FROM t ORDER BY x").fetchall()
    conn.close()
    assert rows == [(1,), (2,), (3,)]


def test_snapshot_db_missing_source_raises(backup_env, tmp_path):
    _, _, bk, _ = backup_env
    with pytest.raises(FileNotFoundError):
        bk.snapshot_db(tmp_path / "nope.db", tmp_path / "out.db")


# -----------------------------------------------------------------------
# collect_snapshot + manifest checksums
# -----------------------------------------------------------------------

def test_collect_snapshot_writes_manifest_with_correct_sha256(backup_env, tmp_path):
    _, _, bk, _ = backup_env
    staging = tmp_path / "stage"
    manifest = bk.collect_snapshot(staging)
    assert manifest["manifest_version"] == 1
    files = {f["name"]: f for f in manifest["files"]}
    # DB and the two small files we seeded should all be present.
    assert "energy.db" in files
    assert "settings.json" in files
    assert "auth.json" in files
    # The encryption key is excluded by default.
    assert ".jackery-creds.key" not in files

    ok, err = bk.verify_manifest(staging)
    assert ok, f"manifest verification failed: {err}"


def test_verify_manifest_detects_tampering(backup_env, tmp_path):
    _, _, bk, _ = backup_env
    staging = tmp_path / "stage"
    bk.collect_snapshot(staging)
    # Corrupt one file post-manifest.
    target = staging / "settings.json"
    target.write_text("TAMPERED")
    ok, err = bk.verify_manifest(staging)
    assert not ok
    assert "checksum mismatch" in (err or "")


def test_collect_snapshot_selective_only_includes_requested(backup_env, tmp_path):
    _, _, bk, _ = backup_env
    staging = tmp_path / "stage"
    manifest = bk.collect_snapshot(staging, selective=["settings.json"])
    names = {f["name"] for f in manifest["files"]}
    assert names == {"settings.json"}


# -----------------------------------------------------------------------
# run_backup + run_restore — full round-trip with a fake mount
# -----------------------------------------------------------------------

def test_run_backup_round_trip_full(backup_env, tmp_path, monkeypatch):
    _data_dir, _work, bk, _bc = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_mount(monkeypatch, bk, fake_remote)

    result = bk.run_backup()
    assert result.ok, f"run_backup failed: {result.error}"
    assert result.files_written >= 3  # db + settings + auth at minimum
    assert result.bytes_written > 0

    # The remote should now have a single snapshot dir under jackery/<stamp>/
    snap_root = fake_remote / "jackery"
    snaps = list(snap_root.iterdir())
    assert len(snaps) == 1
    snap = snaps[0]
    assert (snap / "MANIFEST.json").exists()
    assert (snap / "energy.db").exists()
    assert (snap / "settings.json").exists()


def test_list_remote_snapshots_returns_metadata(backup_env, tmp_path, monkeypatch):
    _, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_mount(monkeypatch, bk, fake_remote)

    bk.run_backup()
    snaps = bk.list_remote_snapshots()
    assert len(snaps) == 1
    s = snaps[0]
    assert s["valid"] is True
    assert "energy.db" in (s.get("files") or [])
    assert s["total_bytes"] > 0


def test_restore_round_trip_full(backup_env, tmp_path, monkeypatch):
    data_dir, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_mount(monkeypatch, bk, fake_remote)

    # 1. Take a backup.
    result = bk.run_backup()
    assert result.ok

    # 2. Wipe the local data dir to simulate a fresh install.
    for p in data_dir.iterdir():
        p.unlink()
    assert not (data_dir / "energy.db").exists()

    # 3. Find the snapshot name the remote is serving.
    snaps = bk.list_remote_snapshots()
    assert len(snaps) == 1
    snap_name = snaps[0]["dir"]

    # 4. Restore.
    out = bk.run_restore(snapshot_dir_name=snap_name, scope={"full": True})
    assert out["ok"], f"run_restore failed: {out.get('error')}"
    assert "energy.db" in out["restored_files"]
    assert "settings.json" in out["restored_files"]

    # 5. Verify the restored DB is queryable and intact.
    conn = sqlite3.connect(str(data_dir / "energy.db"))
    rows = conn.execute("SELECT x FROM t ORDER BY x").fetchall()
    conn.close()
    assert rows == [(1,), (2,), (3,)]
    assert json.loads((data_dir / "settings.json").read_text())["hour"] == 3


def test_restore_selective_only_named_files(backup_env, tmp_path, monkeypatch):
    data_dir, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_mount(monkeypatch, bk, fake_remote)

    bk.run_backup()
    # Wipe.
    for p in data_dir.iterdir():
        p.unlink()
    snap_name = bk.list_remote_snapshots()[0]["dir"]

    out = bk.run_restore(snapshot_dir_name=snap_name,
                         scope={"files": ["settings.json"]})
    assert out["ok"]
    assert out["restored_files"] == ["settings.json"]
    # DB should NOT have been restored.
    assert not (data_dir / "energy.db").exists()
    # Selected file is back.
    assert (data_dir / "settings.json").exists()


def test_run_backup_skips_when_no_credentials(backup_env, tmp_path, monkeypatch):
    _, _, bk, bc = backup_env
    bc.clear()
    result = bk.run_backup()
    assert not result.ok
    assert result.skipped_reason == "no_credentials"


def test_run_backup_handles_remote_corruption(backup_env, tmp_path, monkeypatch):
    """If the remote-side checksum verify fails (e.g. a flaky network
    truncated a file), the partial snapshot must be cleaned up so we
    don't litter the NAS with broken backups."""
    _data_dir, _work, bk, _bc = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_mount(monkeypatch, bk, fake_remote)

    real_copytree = bk.shutil.copytree

    def flaky_copytree(src, dst, *a, **kw):
        real_copytree(src, dst, *a, **kw)
        # Simulate corruption: truncate one file post-copy.
        bad = dst / "settings.json"
        if bad.exists():
            bad.write_text("CORRUPTED")
    monkeypatch.setattr(bk.shutil, "copytree", flaky_copytree)

    result = bk.run_backup()
    assert not result.ok
    assert "verify" in (result.error or "").lower()
    # The corrupt remote dir should have been removed.
    snap_root = fake_remote / "jackery"
    if snap_root.exists():
        assert list(snap_root.iterdir()) == []
