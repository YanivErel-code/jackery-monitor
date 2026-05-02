"""Tests for backup.py — snapshot integrity, manifest checksums,
restore round-trip. The remote SMB transport is replaced by a local
directory (fake_remote) so these tests don't require a NAS, network,
or smbclient binary."""
from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
from pathlib import Path

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


def _patch_smb(monkeypatch, bk, fake_remote: Path):
    """Replace the smbclient helpers in backup.py with thin shims that
    operate on a local directory (`fake_remote`) standing in for the
    SMB share root. Mirrors what smbclient would do for our usage —
    enough surface for snapshot/list/restore round-trips and the
    truncation simulation, without any subprocess work.
    """
    fake_remote.mkdir(parents=True, exist_ok=True)

    def _resolve(remote: str) -> Path:
        # remote paths in the production code are share-root-relative
        # ('jackery/2026-05-02_030000/...'); strip leading slash for
        # safety and join under the fake-remote root.
        return fake_remote / remote.lstrip("/")

    def fake_mkdir(_creds, remote_path):
        _resolve(remote_path).mkdir(parents=True, exist_ok=True)

    def fake_put(_creds, local, remote, **_kw):
        target = _resolve(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)

    def fake_put_dir(_creds, local_dir, remote_dir, **_kw):
        target = _resolve(remote_dir)
        target.mkdir(parents=True, exist_ok=True)
        for child in Path(local_dir).iterdir():
            if child.is_file():
                shutil.copy2(child, target / child.name)

    def fake_get(_creds, remote, local, **_kw):
        src = _resolve(remote)
        if not src.exists():
            raise bk.SMBClientError(
                f"NT_STATUS_OBJECT_NAME_NOT_FOUND opening {remote}")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local)

    def fake_get_text(creds, remote):
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode="r", delete=False) as tf:
            tmp_name = tf.name
        try:
            fake_get(creds, remote, Path(tmp_name))
            return Path(tmp_name).read_text()
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    def fake_delete(_creds, remote):
        target = _resolve(remote)
        target.unlink(missing_ok=True)

    def fake_ls(_creds, remote_dir):
        d = _resolve(remote_dir) if remote_dir else fake_remote
        if not d.exists():
            raise bk.SMBClientError(
                f"NT_STATUS_OBJECT_NAME_NOT_FOUND opening {remote_dir}")
        out = []
        for child in sorted(d.iterdir()):
            out.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else 0,
            })
        return out

    def fake_rmtree(_creds, remote_dir):
        d = _resolve(remote_dir)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(bk, "_smb_mkdir", fake_mkdir)
    monkeypatch.setattr(bk, "_smb_put", fake_put)
    monkeypatch.setattr(bk, "_smb_put_dir", fake_put_dir)
    monkeypatch.setattr(bk, "_smb_get", fake_get)
    monkeypatch.setattr(bk, "_smb_get_text", fake_get_text)
    monkeypatch.setattr(bk, "_smb_delete", fake_delete)
    monkeypatch.setattr(bk, "_smb_ls", fake_ls)
    monkeypatch.setattr(bk, "_smb_rmtree", fake_rmtree)


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
    _patch_smb(monkeypatch, bk, fake_remote)

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
    _patch_smb(monkeypatch, bk, fake_remote)

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
    _patch_smb(monkeypatch, bk, fake_remote)

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
    _patch_smb(monkeypatch, bk, fake_remote)

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


def test_prune_keeps_only_newest_n(backup_env, tmp_path, monkeypatch):
    """Direct test of prune_old_snapshots: seed five timestamped dirs,
    keep_count=2 should leave the two newest and rmtree the three
    older ones."""
    _, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_smb(monkeypatch, bk, fake_remote)
    snap_root = fake_remote / "jackery"
    snap_root.mkdir(parents=True)

    # Five snapshot dirs, oldest to newest by name.
    names = [
        "2026-04-28_030000",
        "2026-04-29_030000",
        "2026-04-30_030000",
        "2026-05-01_030000",
        "2026-05-02_030000",
    ]
    for n in names:
        d = snap_root / n
        d.mkdir()
        (d / "MANIFEST.json").write_text("{}")
        (d / "energy.db").write_text("X")

    # Drop a non-snapshot dir + a stray file alongside — the prune
    # must leave both alone.
    (snap_root / "manual-export").mkdir()
    (snap_root / "manual-export" / "notes.txt").write_text("don't touch me")
    (snap_root / "README.txt").write_text("nor me")

    summary = bk.prune_old_snapshots(keep_count=2)
    assert summary["considered"] == 5
    assert summary["pruned"] == 3
    assert summary["kept"] == 2

    remaining = sorted(p.name for p in snap_root.iterdir())
    # Two newest snapshot dirs survive, plus the manual stuff.
    assert remaining == [
        "2026-05-01_030000",
        "2026-05-02_030000",
        "README.txt",
        "manual-export",
    ]


def test_prune_no_op_when_under_threshold(backup_env, tmp_path, monkeypatch):
    """If keep_count >= number of snapshots, nothing should be deleted."""
    _, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_smb(monkeypatch, bk, fake_remote)
    snap_root = fake_remote / "jackery"
    snap_root.mkdir(parents=True)
    for n in ["2026-05-01_030000", "2026-05-02_030000"]:
        (snap_root / n).mkdir()
        (snap_root / n / "MANIFEST.json").write_text("{}")

    summary = bk.prune_old_snapshots(keep_count=10)
    assert summary["considered"] == 2
    assert summary["pruned"] == 0
    assert summary["kept"] == 2
    assert (snap_root / "2026-05-01_030000").exists()
    assert (snap_root / "2026-05-02_030000").exists()


def test_run_backup_prunes_after_successful_upload(backup_env, tmp_path,
                                                    monkeypatch):
    """End-to-end: with keep_count=1 and a pre-existing snapshot,
    run_backup should land the new snapshot AND remove the old one
    in one go."""
    _, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_smb(monkeypatch, bk, fake_remote)

    # Seed an old snapshot that should be pruned.
    snap_root = fake_remote / "jackery"
    snap_root.mkdir(parents=True)
    old = snap_root / "2026-04-01_030000"
    old.mkdir()
    (old / "MANIFEST.json").write_text("{}")

    result = bk.run_backup(keep_count=1)
    assert result.ok

    remaining = sorted(p.name for p in snap_root.iterdir())
    # Only the just-created snapshot should remain — the seed is gone.
    assert len(remaining) == 1
    assert remaining[0] != "2026-04-01_030000"


def test_run_backup_skips_prune_when_keep_count_none(backup_env, tmp_path,
                                                      monkeypatch):
    """keep_count=None (the default) means 'keep forever' — even ancient
    snapshots stay put."""
    _, _, bk, _ = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_smb(monkeypatch, bk, fake_remote)

    snap_root = fake_remote / "jackery"
    snap_root.mkdir(parents=True)
    old = snap_root / "2020-01-01_030000"
    old.mkdir()
    (old / "MANIFEST.json").write_text("{}")

    result = bk.run_backup()  # no keep_count
    assert result.ok
    # Both snapshots present.
    assert (snap_root / "2020-01-01_030000").exists()


def test_run_backup_skips_when_no_credentials(backup_env, tmp_path, monkeypatch):
    _, _, bk, bc = backup_env
    bc.clear()
    result = bk.run_backup()
    assert not result.ok
    assert result.skipped_reason == "no_credentials"


def test_run_backup_handles_remote_truncation(backup_env, tmp_path, monkeypatch):
    """If the remote-side size check fails (e.g. a flaky network
    truncated a file mid-upload), the partial snapshot must be cleaned
    up so we don't litter the NAS with broken backups. We simulate the
    truncation by wrapping the fake SMB upload to shorten one file
    after it lands on the remote — the post-upload `ls` size compare
    in run_backup catches it and triggers cleanup.
    """
    _data_dir, _work, bk, _bc = backup_env
    fake_remote = tmp_path / "fake-nas"
    _patch_smb(monkeypatch, bk, fake_remote)

    # Wrap the fake _smb_put_dir to truncate one file after upload —
    # mimics a transfer that finished but lost bytes off the end.
    real_put_dir = bk._smb_put_dir

    def flaky_put_dir(creds, local_dir, remote_dir, **kw):
        real_put_dir(creds, local_dir, remote_dir, **kw)
        bad = fake_remote / remote_dir.lstrip("/") / "settings.json"
        if bad.exists():
            bad.write_text("X")  # 1 byte vs. real size — size check fails
    monkeypatch.setattr(bk, "_smb_put_dir", flaky_put_dir)

    result = bk.run_backup()
    assert not result.ok
    assert "verify" in (result.error or "").lower()
    # The corrupt remote dir should have been removed.
    snap_root = fake_remote / "jackery"
    if snap_root.exists():
        assert list(snap_root.iterdir()) == []
