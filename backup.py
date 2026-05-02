"""
Backup & restore — snapshots /data to a remote SMB/CIFS share.

Design (see also docs/backup.md):
  * Daily at 03:00 local (configurable via settings) we mount the remote
    share, take an online SQLite backup of /data/energy.db (consistent
    even with active writers thanks to sqlite3's online .backup API),
    copy the small JSON files alongside it (auth, kasa-creds,
    anthropic-creds, jackery-creds, settings, location), write a
    MANIFEST.json with sha256 checksums, and unmount.
  * The encryption key (.jackery-creds.key) is intentionally NOT
    backed up — see SCOPE_INCLUDES_KEY below. Restoring on a fresh
    install brings back the DB but credentials need to be re-entered.
    Historical telemetry — the bulk of the value — is preserved.
  * Snapshots live in dated directories on the remote
    (YYYY-MM-DD_HHMMSS), kept forever (no automatic deletion).
  * Run history is kept in-memory as a small ring buffer + persisted
    summary JSON at /data/backup-status.json so the UI can show it
    without hitting the remote.

The module exposes pure functions (snapshot_db, write_manifest, etc.)
plus a higher-level run_backup() / run_restore() that compose them.
The async loop in server.py calls run_backup() on schedule.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import backup_creds

log = logging.getLogger("backup")

# ---- file scope ------------------------------------------------------------

DATA_DIR = Path(os.environ.get("JACKERY_DATA_DIR", "/data"))

# Files we always include in a backup. Order matters only for the manifest
# listing; restore reads the manifest, not this list.
DB_FILE = "energy.db"
SMALL_FILES = (
    "auth.json",
    "kasa-creds.json",
    "anthropic-creds.json",
    "jackery-creds.json",
    "settings.json",
    "location.json",
    "anthropic-prefs.json",
    "cost-plan.json",
)

# The at-rest encryption key. NOT backed up by default — see module docstring.
KEY_FILE = ".jackery-creds.key"
SCOPE_INCLUDES_KEY = False  # change only if you've thought hard about it.

STATUS_FILE = DATA_DIR / "backup-status.json"

# Manifest schema version. Bump if you change the on-disk layout in a
# way that older readers can't tolerate.
MANIFEST_VERSION = 1

# How many recent runs to keep in the status JSON's ring buffer.
RUN_HISTORY_LIMIT = 20


# ---- data classes ----------------------------------------------------------


@dataclasses.dataclass
class BackupResult:
    ok: bool
    ts: int                   # epoch seconds when the run started
    iso: str                  # ISO-8601 in local TZ
    snapshot_dir: str | None  # path on the remote, e.g. ".../2026-05-02_030000"
    duration_s: float
    bytes_written: int
    files_written: int
    error: str | None = None
    # Set if the run was aborted because no creds are configured.
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---- low-level helpers -----------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso_local() -> tuple[int, str, str]:
    """Return (epoch, iso-local-timestamp, dir-safe-stamp) for naming
    snapshot directories. Dir stamp avoids ':' so it works on any FS."""
    ts = int(time.time())
    local = _dt.datetime.fromtimestamp(ts).astimezone()
    iso = local.isoformat()
    dir_stamp = local.strftime("%Y-%m-%d_%H%M%S")
    return ts, iso, dir_stamp


def snapshot_db(src_db: Path, dst_db: Path) -> int:
    """Take a consistent online backup of `src_db` to `dst_db`.

    Uses sqlite3's online .backup API — pages are copied while the
    source is live, no need to stop writers. WAL mode is fully
    supported. Returns the size in bytes of the resulting file.

    Source must exist; if it doesn't, raises FileNotFoundError so the
    caller can decide whether to abort the whole run or skip the DB.
    """
    if not src_db.exists():
        raise FileNotFoundError(f"source DB not found: {src_db}")
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    # Ensure no stale dst.
    if dst_db.exists():
        dst_db.unlink()
    src = sqlite3.connect(str(src_db))
    try:
        # Read-only mode would be nice but .backup only works on a
        # full connection. We acquire a shared read lock implicitly,
        # writers can still proceed thanks to WAL.
        dst = sqlite3.connect(str(dst_db))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dst_db.stat().st_size


def collect_snapshot(staging_dir: Path,
                     *,
                     include_key: bool = SCOPE_INCLUDES_KEY,
                     selective: Iterable[str] | None = None) -> dict[str, Any]:
    """Build a complete snapshot under `staging_dir` and write a
    manifest. Returns the manifest dict.

    `selective` (used by tests + future selective-backup option) is an
    iterable of file basenames to include. None means "everything in
    SMALL_FILES + DB_FILE". The encryption key is gated by
    `include_key` regardless.
    """
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    selected = set(selective) if selective is not None else set(
        (DB_FILE, *SMALL_FILES))

    files_meta: list[dict[str, Any]] = []
    total_bytes = 0

    # 1. SQLite online snapshot (if selected and source exists).
    if DB_FILE in selected:
        src_db = DATA_DIR / DB_FILE
        if src_db.exists():
            dst_db = staging_dir / DB_FILE
            size = snapshot_db(src_db, dst_db)
            files_meta.append({
                "name": DB_FILE,
                "size": size,
                "sha256": _sha256_file(dst_db),
            })
            total_bytes += size

    # 2. Small JSON / config files. Each one is optional — if it's
    # missing on the source we just skip it (user may not have
    # configured every integration).
    for name in SMALL_FILES:
        if name not in selected:
            continue
        src = DATA_DIR / name
        if not src.exists():
            continue
        dst = staging_dir / name
        shutil.copy2(src, dst)
        files_meta.append({
            "name": name,
            "size": dst.stat().st_size,
            "sha256": _sha256_file(dst),
        })
        total_bytes += dst.stat().st_size

    # 3. Encryption key — only if the operator explicitly opted in.
    if include_key:
        src = DATA_DIR / KEY_FILE
        if src.exists():
            dst = staging_dir / KEY_FILE
            shutil.copy2(src, dst)
            files_meta.append({
                "name": KEY_FILE,
                "size": dst.stat().st_size,
                "sha256": _sha256_file(dst),
            })
            total_bytes += dst.stat().st_size

    ts, iso, _ = _now_iso_local()
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "ts": ts,
        "iso": iso,
        "include_key": bool(include_key),
        "files": files_meta,
        "total_bytes": total_bytes,
    }
    (staging_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def verify_manifest(snapshot_dir: Path) -> tuple[bool, str | None]:
    """Re-hash every file in the snapshot and compare against the
    manifest. Used after a copy to remote and during restore. Returns
    (ok, error_message_or_None)."""
    snapshot_dir = Path(snapshot_dir)
    mpath = snapshot_dir / "MANIFEST.json"
    if not mpath.exists():
        return False, "MANIFEST.json missing"
    try:
        manifest = json.loads(mpath.read_text())
    except Exception as e:
        return False, f"manifest unreadable: {e}"
    files = manifest.get("files") or []
    if not files:
        return False, "manifest has no files"
    for entry in files:
        name = entry.get("name")
        expected = entry.get("sha256")
        if not name or not expected:
            return False, f"manifest entry malformed: {entry}"
        f = snapshot_dir / name
        if not f.exists():
            return False, f"file missing in snapshot: {name}"
        got = _sha256_file(f)
        if got != expected:
            return False, f"checksum mismatch on {name}"
    return True, None


# ---- SMB mount / copy ------------------------------------------------------


class CIFSMountError(RuntimeError):
    """mount.cifs failed (host unreachable, bad creds, no share, etc)."""


@contextlib.contextmanager
def mount_cifs(host: str, share: str, username: str, password: str,
               *, domain: str = "WORKGROUP",
               mountpoint: Path | None = None,
               timeout_s: float = 20.0):
    """Mount //host/share at a temporary mountpoint, yield the path,
    and unmount on exit. mount.cifs needs CAP_SYS_ADMIN inside the
    container.

    `share` may be either a share-name ("backups") or a path-style
    ("/volume1/backups") — we strip the leading slash because Synology
    SMB shares are rooted at the share name itself, not the volume
    path. The remote path inside the share is handled separately by
    the caller (subdir).
    """
    cleanup_mount = mountpoint is None
    if mountpoint is None:
        mountpoint = Path(tempfile.mkdtemp(prefix="jackery-bak-"))
    else:
        mountpoint.mkdir(parents=True, exist_ok=True)

    # Synology shares: strip leading slash, take only the first segment
    # as the share name. Anything after is mounted-path inside.
    share_clean = share.lstrip("/")
    # If the operator gave a multi-segment path, only the first segment
    # is the share name; the rest is a sub-path that we'll address via
    # the subdir parameter at copy time.
    share_name = share_clean.split("/")[0] if share_clean else share_clean
    mount_extra_subpath = "/".join(share_clean.split("/")[1:])

    # Build the credentials file in /tmp so the password doesn't show
    # up in `ps`. Mode 0600.
    creds_path = Path(tempfile.mkstemp(prefix="jackery-bak-cred-")[1])
    try:
        creds_path.write_text(
            f"username={username}\npassword={password}\ndomain={domain}\n")
        creds_path.chmod(0o600)
        unc = f"//{host}/{share_name}"
        cmd = [
            "mount", "-t", "cifs", unc, str(mountpoint),
            "-o", f"credentials={creds_path},vers=3.0,iocharset=utf8,"
                  "rw,uid=0,gid=0,file_mode=0600,dir_mode=0700,nounix",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as e:
            raise CIFSMountError(f"mount timed out: {e}") from e
        except FileNotFoundError as e:
            raise CIFSMountError(
                "mount.cifs not installed in container — "
                "install cifs-utils") from e
        if r.returncode != 0:
            raise CIFSMountError(
                f"mount failed (code {r.returncode}): "
                f"{r.stderr.strip() or r.stdout.strip()}")

        if mount_extra_subpath:
            yield mountpoint / mount_extra_subpath
        else:
            yield mountpoint
    finally:
        # Unmount even if the body raised. Use lazy unmount as a
        # fallback so a stuck handle doesn't strand the mountpoint.
        try:
            subprocess.run(["umount", str(mountpoint)],
                           capture_output=True, timeout=10, check=False)
        except Exception:
            pass
        # Always clean creds file.
        try:
            creds_path.unlink(missing_ok=True)
        except Exception:
            pass
        if cleanup_mount:
            try:
                # If unmount actually succeeded the dir is empty; rmtree
                # is safe. If it didn't, rmtree might fail silently —
                # not a leak risk, just one stray /tmp dir per failure.
                shutil.rmtree(mountpoint, ignore_errors=True)
            except Exception:
                pass


def test_connectivity(creds: dict | None = None,
                      *, timeout_s: float = 15.0) -> dict[str, Any]:
    """Mount the remote, write a tiny probe file, read it back, delete
    it, unmount. Returns {ok, latency_ms, error?}.

    Designed to run synchronously from the API handler (it's quick —
    a few seconds). The handler should call this in a thread to avoid
    blocking the asyncio loop.
    """
    creds = creds or backup_creds.load()
    if not creds:
        return {"ok": False, "error": "no_credentials"}
    started = time.time()
    try:
        with mount_cifs(
            host=creds["host"], share=creds["share"],
            username=creds["username"], password=creds["password"],
            domain=creds.get("domain") or "WORKGROUP",
            timeout_s=timeout_s,
        ) as mp:
            subdir = (creds.get("subdir") or "").lstrip("/")
            target = (mp / subdir) if subdir else mp
            target.mkdir(parents=True, exist_ok=True)
            probe = target / f".jackery-probe-{int(started)}"
            probe.write_text("ok")
            content = probe.read_text()
            probe.unlink(missing_ok=True)
            if content != "ok":
                return {"ok": False, "error": "probe_roundtrip_failed"}
        return {"ok": True, "latency_ms": int((time.time() - started) * 1000)}
    except CIFSMountError as e:
        return {"ok": False, "error": f"mount_failed: {e}"}
    except PermissionError as e:
        return {"ok": False, "error": f"permission_denied: {e}"}
    except Exception as e:
        log.exception("backup connectivity test failed")
        return {"ok": False, "error": str(e)}


# ---- top-level run ---------------------------------------------------------


def _load_status() -> dict[str, Any]:
    try:
        return json.loads(STATUS_FILE.read_text())
    except FileNotFoundError:
        return {"runs": [], "last_ok_ts": None}
    except Exception as e:
        log.warning("backup status JSON unreadable, resetting: %s", e)
        return {"runs": [], "last_ok_ts": None}


def _save_status(status: dict[str, Any]) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, indent=2, sort_keys=True))
        os.replace(tmp, STATUS_FILE)
    except Exception as e:
        log.warning("failed to persist backup status: %s", e)


def _record_run(result: BackupResult) -> None:
    s = _load_status()
    runs = list(s.get("runs") or [])
    runs.insert(0, result.as_dict())
    runs = runs[:RUN_HISTORY_LIMIT]
    s["runs"] = runs
    if result.ok:
        s["last_ok_ts"] = result.ts
    _save_status(s)


def list_remote_snapshots(creds: dict | None = None) -> list[dict[str, Any]]:
    """Mount the remote and list every snapshot directory inside the
    user's subdir. Each entry has {dir, ts, iso, total_bytes, files}
    drawn from MANIFEST.json (or marked invalid if the manifest is
    missing).
    """
    creds = creds or backup_creds.load()
    if not creds:
        return []
    out: list[dict[str, Any]] = []
    try:
        with mount_cifs(
            host=creds["host"], share=creds["share"],
            username=creds["username"], password=creds["password"],
            domain=creds.get("domain") or "WORKGROUP",
        ) as mp:
            subdir = (creds.get("subdir") or "").lstrip("/")
            root = (mp / subdir) if subdir else mp
            if not root.exists():
                return []
            for entry in sorted(root.iterdir(), reverse=True):
                if not entry.is_dir():
                    continue
                # Skip hidden / temp dirs.
                if entry.name.startswith("."):
                    continue
                manifest_path = entry / "MANIFEST.json"
                if not manifest_path.exists():
                    out.append({
                        "dir": entry.name, "valid": False,
                        "error": "manifest_missing",
                    })
                    continue
                try:
                    m = json.loads(manifest_path.read_text())
                    out.append({
                        "dir": entry.name,
                        "valid": True,
                        "ts": m.get("ts"),
                        "iso": m.get("iso"),
                        "total_bytes": m.get("total_bytes"),
                        "files": [f.get("name") for f in (m.get("files") or [])],
                        "include_key": bool(m.get("include_key")),
                    })
                except Exception as e:
                    out.append({
                        "dir": entry.name, "valid": False,
                        "error": f"manifest_unreadable: {e}",
                    })
    except CIFSMountError as e:
        log.warning("list snapshots: mount failed: %s", e)
    except Exception:
        log.exception("list snapshots failed")
    return out


def run_backup(*, creds: dict | None = None,
               include_key: bool | None = None) -> BackupResult:
    """Synchronous, end-to-end backup. Safe to call from a thread.

    Steps:
      1. Resolve creds (param > stored).
      2. Stage the snapshot in a tempdir.
      3. Mount remote, copy staged dir, verify checksums.
      4. Unmount.
      5. Persist run history.
    """
    started = time.time()
    ts, iso, dir_stamp = _now_iso_local()
    creds = creds or backup_creds.load()
    if not creds:
        result = BackupResult(
            ok=False, ts=ts, iso=iso, snapshot_dir=None,
            duration_s=0.0, bytes_written=0, files_written=0,
            skipped_reason="no_credentials",
        )
        _record_run(result)
        return result

    use_key = SCOPE_INCLUDES_KEY if include_key is None else bool(include_key)

    # Stage locally first, only ship to remote if everything succeeded.
    staging_root = Path(tempfile.mkdtemp(prefix="jackery-bak-stage-"))
    try:
        staging_dir = staging_root / dir_stamp
        try:
            manifest = collect_snapshot(staging_dir, include_key=use_key)
        except Exception as e:
            log.exception("snapshot staging failed")
            result = BackupResult(
                ok=False, ts=ts, iso=iso, snapshot_dir=None,
                duration_s=time.time() - started,
                bytes_written=0, files_written=0,
                error=f"snapshot_failed: {e}",
            )
            _record_run(result)
            return result

        # Self-check the staged snapshot before uploading. Catching a
        # bad sha256 locally beats discovering it after a 50MB upload.
        ok, err = verify_manifest(staging_dir)
        if not ok:
            log.error("staged snapshot failed self-verify: %s", err)
            result = BackupResult(
                ok=False, ts=ts, iso=iso, snapshot_dir=None,
                duration_s=time.time() - started,
                bytes_written=0, files_written=0,
                error=f"self_verify_failed: {err}",
            )
            _record_run(result)
            return result

        # Mount + copy.
        try:
            with mount_cifs(
                host=creds["host"], share=creds["share"],
                username=creds["username"], password=creds["password"],
                domain=creds.get("domain") or "WORKGROUP",
            ) as mp:
                subdir = (creds.get("subdir") or "").lstrip("/")
                target_root = (mp / subdir) if subdir else mp
                target_root.mkdir(parents=True, exist_ok=True)
                target_dir = target_root / dir_stamp
                # Copy via shutil — small file count, no need for rsync.
                shutil.copytree(staging_dir, target_dir)
                # Verify the remote copy matches.
                ok, err = verify_manifest(target_dir)
                if not ok:
                    # Don't leave a corrupt remote snapshot.
                    shutil.rmtree(target_dir, ignore_errors=True)
                    raise RuntimeError(f"remote_verify_failed: {err}")
        except CIFSMountError as e:
            result = BackupResult(
                ok=False, ts=ts, iso=iso, snapshot_dir=None,
                duration_s=time.time() - started,
                bytes_written=0, files_written=0,
                error=f"mount_failed: {e}",
            )
            _record_run(result)
            return result
        except Exception as e:
            log.exception("backup upload failed")
            result = BackupResult(
                ok=False, ts=ts, iso=iso, snapshot_dir=None,
                duration_s=time.time() - started,
                bytes_written=0, files_written=0,
                error=f"upload_failed: {e}",
            )
            _record_run(result)
            return result

        result = BackupResult(
            ok=True, ts=ts, iso=iso,
            snapshot_dir=f"{(creds.get('subdir') or '').strip('/')}/{dir_stamp}",
            duration_s=time.time() - started,
            bytes_written=int(manifest.get("total_bytes") or 0),
            files_written=len(manifest.get("files") or []),
        )
        _record_run(result)
        return result
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def run_restore(*, snapshot_dir_name: str,
                scope: dict[str, Any] | None = None,
                creds: dict | None = None) -> dict[str, Any]:
    """Restore the named snapshot from the remote into /data.

    `scope`:
      None or {"full": True}        -> restore everything in the manifest.
      {"files": ["energy.db", ...]} -> selective: only listed basenames.

    Existing files in /data are overwritten in-place. The DB is
    restored via a swap-and-replace so the live app can keep reading
    until the instant we move the new file into place. Caller should
    restart the app afterwards (the API handler does this).

    The encryption key (.jackery-creds.key) is restored only if it's
    in the manifest AND scope explicitly includes it.
    """
    creds = creds or backup_creds.load()
    if not creds:
        return {"ok": False, "error": "no_credentials"}

    files_filter: set[str] | None = None
    if scope and not scope.get("full"):
        listed = scope.get("files")
        if listed:
            files_filter = {str(x) for x in listed}

    try:
        with mount_cifs(
            host=creds["host"], share=creds["share"],
            username=creds["username"], password=creds["password"],
            domain=creds.get("domain") or "WORKGROUP",
        ) as mp:
            subdir = (creds.get("subdir") or "").lstrip("/")
            root = (mp / subdir) if subdir else mp
            snapshot = root / snapshot_dir_name
            if not snapshot.exists():
                return {"ok": False,
                        "error": f"snapshot_not_found: {snapshot_dir_name}"}
            ok, err = verify_manifest(snapshot)
            if not ok:
                return {"ok": False, "error": f"verify_failed: {err}"}

            manifest = json.loads(
                (snapshot / "MANIFEST.json").read_text())

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            restored: list[str] = []
            for entry in (manifest.get("files") or []):
                name = entry.get("name")
                if not name:
                    continue
                if files_filter is not None and name not in files_filter:
                    continue
                # Skip the key file unless explicitly opted in via scope.
                if name == KEY_FILE:
                    if not (scope and scope.get("include_key")):
                        continue
                src = snapshot / name
                dst = DATA_DIR / name
                # Swap-and-replace for atomic-ish replacement.
                tmp = dst.with_suffix(dst.suffix + ".restoring")
                shutil.copy2(src, tmp)
                # On Linux os.replace is atomic when src/dst on the
                # same filesystem (they are — both /data).
                os.replace(tmp, dst)
                # Tighten perms on credentials/key files.
                if name.endswith(".json") or name == KEY_FILE:
                    try:
                        os.chmod(dst, 0o600)
                    except Exception:
                        pass
                restored.append(name)

            return {
                "ok": True,
                "restored_files": restored,
                "snapshot": snapshot_dir_name,
                "manifest_iso": manifest.get("iso"),
            }
    except CIFSMountError as e:
        return {"ok": False, "error": f"mount_failed: {e}"}
    except Exception as e:
        log.exception("restore failed")
        return {"ok": False, "error": str(e)}


# ---- async scheduler -------------------------------------------------------


def _seconds_until_next_run(target_hour: int) -> float:
    """Compute seconds until the next occurrence of HH:00 in the
    current local timezone. If it's already past today's HH:00, returns
    seconds until tomorrow's. Out-of-range hour falls back to 03:00.
    """
    try:
        h = int(target_hour)
        if not (0 <= h < 24):
            raise ValueError("hour out of range")
    except Exception:
        h = 3
    now = _dt.datetime.now().astimezone()
    target = now.replace(hour=h, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + _dt.timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


async def backup_loop(get_schedule: Any | None = None) -> None:
    """Background loop. Sleeps until the configured daily hour, then
    runs a backup (in a thread, since SMB mount + sqlite copy block).

    `get_schedule` is a callable that returns the current target hour
    (int 0-23 in local time). Wired to settings.get(...) by server.py
    so the user can change the hour at runtime without restarting.
    Defaults to 03:00.
    """
    if get_schedule is None:
        get_schedule = lambda: 3  # noqa: E731

    while True:
        try:
            schedule = get_schedule()
            wait_s = _seconds_until_next_run(schedule)
            log.info("backup loop: next run in %.1fh (at %02d:00 local)",
                     wait_s / 3600, int(schedule) if schedule is not None else 3)
            await asyncio.sleep(wait_s)
            # Do the work in a thread so sqlite + mount don't block
            # the event loop.
            log.info("backup loop: starting scheduled run")
            result = await asyncio.to_thread(run_backup)
            if result.ok:
                log.info("backup loop: ok (%d files, %d bytes, %.1fs)",
                         result.files_written, result.bytes_written,
                         result.duration_s)
            elif result.skipped_reason:
                log.info("backup loop: skipped (%s)", result.skipped_reason)
            else:
                log.warning("backup loop: failed (%s)", result.error)
        except asyncio.CancelledError:
            log.info("backup loop: cancelled")
            raise
        except Exception:
            log.exception("backup loop iteration failed")
            # Wait a bit before retrying so we don't spin on a
            # systemic error.
            await asyncio.sleep(300)


def get_status() -> dict[str, Any]:
    """Read the current persisted status — UI-facing summary."""
    s = _load_status()
    creds = backup_creds.public_view()
    return {
        "configured": creds is not None,
        "remote": creds,
        "runs": s.get("runs") or [],
        "last_ok_ts": s.get("last_ok_ts"),
    }
