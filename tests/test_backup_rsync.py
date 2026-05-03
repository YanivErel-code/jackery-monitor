"""Tests for the rsync transport in backup.py.

We don't have a real rsync binary or SSH server available in CI, so the
strategy is identical to test_backup.py's SMB tests: replace the
high-level transport ops (push/pull/list/rmtree) with shims that
operate on a local 'fake remote' directory. The dispatcher logic in
backup.py still routes by creds["transport"] but the I/O is
local-filesystem.

What we DO exercise with real binaries:
  * The rsync command-line construction (when an actual subprocess
    invocation is asserted, e.g. the --link-dest test below — we
    capture the args list rather than actually running rsync).
  * The SSH key tempfile lifecycle (creation, mode, deletion) — also
    via a wrapped _rsync_run that records and returns success.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Common fixture: tmp data dir + saved rsync_ssh creds + a fake remote
# directory we redirect all rsync ops at.
# ---------------------------------------------------------------------------


@pytest.fixture()
def rsync_env(tmp_path, monkeypatch):
    """Wire DATA_DIR + creds file at tmp paths and reload modules so
    their module-level constants pick up the new paths. Saves
    rsync_ssh creds (default for these tests; overrideable by callers
    that want rsyncd) and seeds a small DB + a couple of small files
    so the snapshot has real content."""
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

    target_dir = tmp_path / "fake-nas-tree"
    target_dir.mkdir()
    bc.save(
        transport="rsync_ssh",
        host="nas.local",
        ssh_user="backupuser",
        ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEY\n-----END OPENSSH PRIVATE KEY-----",
        target_dir=str(target_dir),
    )

    db_path = data_dir / "energy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1),(2),(3);")
    conn.commit()
    conn.close()
    (data_dir / "settings.json").write_text(json.dumps({"hour": 3}))
    (data_dir / "auth.json").write_text(json.dumps({"user": "alice"}))

    return data_dir, target_dir, bk, bc


def _patch_rsync(monkeypatch, bk, fake_remote: Path):
    """Replace rsync's high-level helpers with local-filesystem shims.
    Mirrors the SMB _patch_smb pattern: enough surface to exercise
    snapshot/list/restore round-trips without invoking subprocess."""

    def _resolve(creds, subpath: str) -> Path:
        # rsync_ssh creds carry an absolute target_dir we already
        # set to point at fake_remote; rsyncd creds (used by a
        # couple of tests) build a URL-style path. For both, the
        # 'subpath' is what we want under fake_remote.
        sub = (subpath or "").lstrip("/")
        return fake_remote / sub if sub else fake_remote

    def fake_list(creds, subpath: str = ""):
        d = _resolve(creds, subpath)
        if not d.exists():
            raise bk.RsyncError(
                f"rsync: change_dir \"{d}\" failed: No such file or directory")
        out = []
        for child in sorted(d.iterdir()):
            out.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else 0,
            })
        return out

    def fake_pull(creds, subpath: str, local: Path, **_kw):
        src = _resolve(creds, subpath)
        if not src.exists():
            raise bk.RsyncError(
                f"rsync: link_stat \"{src}\" failed: No such file or directory")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local)

    def fake_pull_text(creds, subpath: str):
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode="r", delete=False) as tf:
            tmp_name = tf.name
        try:
            fake_pull(creds, subpath, Path(tmp_name))
            return Path(tmp_name).read_text()
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    def fake_push_file(creds, local: Path, subpath: str, **_kw):
        dst = _resolve(creds, subpath)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dst)

    def fake_push_dir(creds, local_dir: Path, subpath: str,
                      *, link_dest=None, **_kw):
        dst = _resolve(creds, subpath)
        dst.mkdir(parents=True, exist_ok=True)
        # We DON'T actually replicate hardlink semantics here — the
        # purpose of these shims is to exercise the upper-layer logic.
        # The link_dest argument is recorded on the function so tests
        # can assert it was passed through correctly.
        fake_push_dir.last_link_dest = link_dest
        for child in Path(local_dir).iterdir():
            if child.is_file():
                shutil.copy2(child, dst / child.name)
    fake_push_dir.last_link_dest = "<unset>"

    def fake_rmtree(creds, subpath: str):
        # The real implementation runs `ssh ... rm -rf <path>`, which
        # works on both files and directories. Mirror that here.
        if bk._transport_of(creds) != "rsync_ssh":
            raise bk.RsyncError("remote rmtree not supported for rsyncd")
        d = _resolve(creds, subpath)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        elif d.exists():
            d.unlink()

    monkeypatch.setattr(bk, "_rsync_list", fake_list)
    monkeypatch.setattr(bk, "_rsync_pull", fake_pull)
    monkeypatch.setattr(bk, "_rsync_pull_text", fake_pull_text)
    monkeypatch.setattr(bk, "_rsync_push_file", fake_push_file)
    monkeypatch.setattr(bk, "_rsync_push_dir", fake_push_dir)
    monkeypatch.setattr(bk, "_rsync_remote_rmtree", fake_rmtree)
    return fake_push_dir


# ---------------------------------------------------------------------------
# Connectivity probe
# ---------------------------------------------------------------------------

def test_rsync_ssh_connectivity_round_trip(rsync_env, monkeypatch):
    _, fake_remote, bk, _ = rsync_env
    _patch_rsync(monkeypatch, bk, fake_remote)
    out = bk.test_connectivity()
    assert out["ok"], f"connectivity failed: {out.get('error')}"
    # Probe should have been cleaned up (rsync_ssh transport).
    leftover = list(fake_remote.glob(".jackery-probe-*"))
    assert leftover == []


# ---------------------------------------------------------------------------
# run_backup — round-trip + link-dest behaviour
# ---------------------------------------------------------------------------

def test_rsync_run_backup_first_run_no_link_dest(rsync_env, monkeypatch):
    """First snapshot — no previous one exists, so --link-dest must
    NOT be passed to rsync. The fake_push_dir shim records the value
    so we can assert it directly."""
    _, fake_remote, bk, _ = rsync_env
    push = _patch_rsync(monkeypatch, bk, fake_remote)

    result = bk.run_backup()
    assert result.ok, f"run_backup failed: {result.error}"
    assert push.last_link_dest is None
    snaps = list(fake_remote.iterdir())
    assert len(snaps) == 1
    snap = snaps[0]
    assert (snap / "MANIFEST.json").exists()
    assert (snap / "energy.db").exists()


def test_rsync_run_backup_second_run_passes_link_dest(rsync_env,
                                                      monkeypatch):
    """When a previous snapshot exists on the remote, the next backup
    must pass --link-dest=../<prev>/ so rsync can hardlink unchanged
    files. We assert via the shim that link_dest was set to the
    earlier snapshot's directory name."""
    _, fake_remote, bk, _ = rsync_env
    push = _patch_rsync(monkeypatch, bk, fake_remote)

    first = bk.run_backup()
    assert first.ok
    first_dir = first.snapshot_dir
    assert first_dir is not None

    second = bk.run_backup()
    assert second.ok
    assert push.last_link_dest == first_dir, (
        f"expected link_dest={first_dir!r}, got {push.last_link_dest!r}")


def test_rsync_run_backup_link_dest_relative_arg(rsync_env, monkeypatch):
    """The actual --link-dest CLI flag must be RELATIVE (../<prev>/)
    so it works regardless of where the target is mounted. We hook
    _rsync_run directly to capture the arg list."""
    _, _, bk, _ = rsync_env

    captured: list[list[str]] = []

    def fake_rsync_run(creds, args, *, timeout_s=600.0):
        captured.append(list(args))
        # Pretend success
        cp = subprocess.CompletedProcess(args=args, returncode=0,
                                         stdout="", stderr="")
        return cp

    monkeypatch.setattr(bk, "_rsync_run", fake_rsync_run)

    # Seed a 'previous' snapshot dir so _list_remote_snapshots_rsync
    # finds something. We'll do this by faking _rsync_list to return
    # a single valid snapshot, and _rsync_pull_text to return a
    # plausible MANIFEST.json for it.
    prev_name = "2026-04-01_030000"
    monkeypatch.setattr(bk, "_rsync_list",
                        lambda creds, subpath="": [
                            {"name": prev_name, "is_dir": True, "size": 0},
                        ])
    monkeypatch.setattr(bk, "_rsync_pull_text",
                        lambda creds, subpath: json.dumps({
                            "manifest_version": 1,
                            "ts": 0, "iso": "2026-04-01T03:00:00",
                            "include_key": False,
                            "files": [{"name": "energy.db",
                                       "size": 0, "sha256": "x"}],
                            "total_bytes": 0,
                        }))
    monkeypatch.setattr(bk, "_verify_rsync_sizes",
                        lambda entries, manifest: None)

    result = bk.run_backup()
    assert result.ok, result.error

    # Find the rsync invocation that pushed the snapshot dir (has both
    # `-a` and a path ending in /<dir_stamp>/). Should include
    # --link-dest=../<prev>/.
    push_args = next((a for a in captured
                      if any("--link-dest=" in s for s in a)), None)
    assert push_args is not None, (
        f"no --link-dest invocation captured among {captured!r}")
    flag = next(s for s in push_args if s.startswith("--link-dest="))
    assert flag == f"--link-dest=../{prev_name}/", (
        f"unexpected link-dest value: {flag!r}")


# ---------------------------------------------------------------------------
# list_remote_snapshots / restore
# ---------------------------------------------------------------------------

def test_rsync_list_remote_snapshots(rsync_env, monkeypatch):
    _, fake_remote, bk, _ = rsync_env
    _patch_rsync(monkeypatch, bk, fake_remote)
    bk.run_backup()
    snaps = bk.list_remote_snapshots()
    assert len(snaps) == 1
    s = snaps[0]
    assert s["valid"] is True
    assert "energy.db" in (s.get("files") or [])
    assert s["total_bytes"] > 0


def test_rsync_restore_round_trip_full(rsync_env, monkeypatch):
    data_dir, fake_remote, bk, _ = rsync_env
    _patch_rsync(monkeypatch, bk, fake_remote)
    result = bk.run_backup()
    assert result.ok

    # Wipe the local data dir to simulate a fresh install.
    for p in data_dir.iterdir():
        p.unlink()
    assert not (data_dir / "energy.db").exists()

    snap_name = bk.list_remote_snapshots()[0]["dir"]
    out = bk.run_restore(snapshot_dir_name=snap_name, scope={"full": True})
    assert out["ok"], f"restore failed: {out.get('error')}"
    assert "energy.db" in out["restored_files"]
    assert "settings.json" in out["restored_files"]

    conn = sqlite3.connect(str(data_dir / "energy.db"))
    rows = conn.execute("SELECT x FROM t ORDER BY x").fetchall()
    conn.close()
    assert rows == [(1,), (2,), (3,)]


def test_rsync_restore_selective(rsync_env, monkeypatch):
    data_dir, fake_remote, bk, _ = rsync_env
    _patch_rsync(monkeypatch, bk, fake_remote)
    bk.run_backup()
    for p in data_dir.iterdir():
        p.unlink()
    snap_name = bk.list_remote_snapshots()[0]["dir"]

    out = bk.run_restore(snapshot_dir_name=snap_name,
                         scope={"files": ["settings.json"]})
    assert out["ok"]
    assert out["restored_files"] == ["settings.json"]
    assert not (data_dir / "energy.db").exists()
    assert (data_dir / "settings.json").exists()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_rsync_ssh_prune_keeps_only_newest_n(rsync_env, monkeypatch):
    _, fake_remote, bk, _ = rsync_env
    _patch_rsync(monkeypatch, bk, fake_remote)

    names = [
        "2026-04-28_030000",
        "2026-04-29_030000",
        "2026-04-30_030000",
        "2026-05-01_030000",
        "2026-05-02_030000",
    ]
    for n in names:
        d = fake_remote / n
        d.mkdir()
        (d / "MANIFEST.json").write_text("{}")

    # Drop a non-snapshot dir + a stray file alongside — must be left.
    (fake_remote / "manual-export").mkdir()
    (fake_remote / "manual-export" / "notes.txt").write_text("don't touch me")
    (fake_remote / "README.txt").write_text("nor me")

    summary = bk.prune_old_snapshots(keep_count=2)
    assert summary["considered"] == 5
    assert summary["pruned"] == 3
    assert summary["kept"] == 2

    remaining = sorted(p.name for p in fake_remote.iterdir())
    assert remaining == [
        "2026-05-01_030000",
        "2026-05-02_030000",
        "README.txt",
        "manual-export",
    ]


def test_rsyncd_prune_returns_no_remote_delete_marker(rsync_env, monkeypatch):
    """rsyncd has no remote-delete primitive — prune_old_snapshots must
    refuse cleanly with a hint instead of pretending to succeed."""
    _, _, bk, bc = rsync_env
    bc.save(
        transport="rsyncd",
        host="nas.local",
        rsync_module="backups",
        rsyncd_user="alice",
        rsyncd_password="hunter2",
        target_subpath="jackery",
    )
    summary = bk.prune_old_snapshots(keep_count=2)
    assert summary["pruned"] == 0
    assert summary.get("error") == "rsyncd_no_remote_delete"
    assert "rsync_ssh" in (summary.get("hint") or "")


# ---------------------------------------------------------------------------
# SSH key tempfile lifecycle
# ---------------------------------------------------------------------------

def test_rsync_ssh_keyfile_is_0600_and_cleaned_up(rsync_env, monkeypatch):
    """_rsync_run materialises the SSH key into a 0600 tempfile, runs
    rsync, then unlinks the file in a finally — even on rsync failure.
    We assert by recording the tempfile path during the run and
    checking it doesn't exist afterwards, both for success and for
    a forced failure."""
    _, _, bk, _ = rsync_env
    seen: list[str] = []

    def fake_subprocess_run(cmd, **kw):
        # The -e arg should reference a freshly-created keyfile.
        for i, c in enumerate(cmd):
            if c == "-e":
                ssh_arg = cmd[i + 1]
                # ssh -i <path> -o ...
                parts = ssh_arg.split()
                key_idx = parts.index("-i") + 1
                key_path = parts[key_idx]
                seen.append(key_path)
                # While we're inside the subprocess call, the file
                # MUST exist with mode 0600.
                assert os.path.exists(key_path), (
                    f"keyfile {key_path} should exist during rsync run")
                mode = os.stat(key_path).st_mode & 0o777
                assert mode == 0o600, f"expected 0600, got {oct(mode)}"
                break
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    creds = bk.backup_creds.load()
    bk._rsync_run(creds, ["-a", "/tmp/x", "user@host:/dst/"])

    assert len(seen) == 1
    assert not os.path.exists(seen[0]), (
        f"keyfile {seen[0]} should be cleaned up after rsync run")


def test_rsync_ssh_keyfile_cleaned_up_on_failure(rsync_env, monkeypatch):
    """Forced rsync failure (timeout) — keyfile must still be unlinked
    by the finally block, otherwise we'd leak private keys in /tmp on
    every transient network blip."""
    _, _, bk, _ = rsync_env
    seen: list[str] = []

    def fake_subprocess_run(cmd, **kw):
        # Capture and then raise as if rsync timed out.
        for i, c in enumerate(cmd):
            if c == "-e":
                ssh_arg = cmd[i + 1]
                parts = ssh_arg.split()
                seen.append(parts[parts.index("-i") + 1])
                break
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    creds = bk.backup_creds.load()
    with pytest.raises(bk.RsyncError):
        bk._rsync_run(creds, ["-a", "/tmp/x", "user@host:/dst/"])

    assert seen, "expected to observe at least one keyfile path"
    for path in seen:
        assert not os.path.exists(path), (
            f"keyfile {path} leaked after rsync timeout")


# ---------------------------------------------------------------------------
# Listing parser
# ---------------------------------------------------------------------------

def test_rsync_ls_line_parses_dir_and_file_rows(rsync_env):
    """The _RSYNC_LS_LINE regex needs to handle both directory rows
    (drwx...) and file rows (-rw...) plus the locale comma in size
    that some rsync builds emit."""
    _, _, bk, _ = rsync_env
    samples = {
        "drwxr-xr-x          4,096 2026/05/02 03:00:00 2026-05-02_030000": (
            "2026-05-02_030000", True, 4096),
        "-rw-r--r--      1,234,567 2026/05/02 03:00:00 energy.db": (
            "energy.db", False, 1234567),
        "-rw-------            512 2026/05/02 03:00:00 settings.json": (
            "settings.json", False, 512),
    }
    for line, (name, is_dir, size) in samples.items():
        m = bk._RSYNC_LS_LINE.match(line)
        assert m is not None, f"failed to match: {line!r}"
        assert m.group("name").strip() == name
        assert (m.group("type") == "d") == is_dir
        assert int(m.group("size").replace(",", "")) == size


# ---------------------------------------------------------------------------
# rsync_ssh remote rm safety
# ---------------------------------------------------------------------------

def test_rsync_remote_rmtree_refuses_paths_outside_target(rsync_env,
                                                          monkeypatch):
    """Defence in depth: even with a path manipulation, rmtree must
    refuse to operate outside the configured target_dir."""
    _, _, bk, _ = rsync_env
    creds = bk.backup_creds.load()
    creds["target_dir"] = "/safe/target"

    with pytest.raises(bk.RsyncError, match="refusing"):
        bk._rsync_remote_rmtree(creds, "../../etc/passwd")
