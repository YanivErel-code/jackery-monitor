"""
Backup & restore — snapshots /data to a remote SMB share.

Design (see also docs/backup.md):
  * Daily at 03:00 local (configurable via settings) we open an SMB
    session to the remote share, take an online SQLite backup of
    /data/energy.db (consistent even with active writers thanks to
    sqlite3's online .backup API), copy the small JSON files alongside
    it (auth, kasa-creds, anthropic-creds, jackery-creds, settings,
    location), write a MANIFEST.json with sha256 checksums, then
    upload the whole staging directory.
  * Transport is `smbclient` from the samba package — pure userspace.
    We deliberately do NOT use `mount.cifs`, which requires
    CAP_SYS_ADMIN + capset() seccomp permission inside the container
    and tends to fail on Synology with "Unable to apply new capability
    set." smbclient avoids the kernel mount path entirely; the
    container can stay fully unprivileged.
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
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import re
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


def _verify_remote_sizes(creds: dict, target_dir: str,
                          manifest: dict) -> str | None:
    """After upload, compare each file's remote size against the manifest.
    Cheap (one ls round trip, no file re-download) but catches the most
    common corruption modes: truncation, missing files. Byte-level
    corruption mid-file is left to SMB's transport CRC + the next
    restore-time verify_manifest. Returns None on success or an error
    string on mismatch.
    """
    expected = {f.get("name"): int(f.get("size") or 0)
                for f in (manifest.get("files") or [])
                if f.get("name")}
    # Manifest itself is also uploaded but isn't in `files`; add it.
    expected["MANIFEST.json"] = -1  # presence-only check
    try:
        entries = _smb_ls(creds, target_dir)
    except SMBClientError as e:
        return f"remote ls failed: {e}"
    by_name = {e["name"]: e for e in entries}
    for name, exp_size in expected.items():
        e = by_name.get(name)
        if e is None:
            return f"file missing on remote: {name}"
        if exp_size >= 0 and e["size"] != exp_size:
            return (f"size mismatch on {name}: "
                    f"expected {exp_size}, got {e['size']}")
    return None


# ---- SMB transport (userspace, via smbclient) -----------------------------


class SMBClientError(RuntimeError):
    """smbclient invocation failed (auth, network, protocol, etc.).

    Replaces the old CIFSMountError. Kept as a separate class so
    existing callers can still distinguish 'transport failed' from
    'snapshot logic failed'.
    """


# Alias for backwards-compat with any tests / external callers that
# still import the old name. New code should use SMBClientError.
CIFSMountError = SMBClientError


def _split_share(share_field: str) -> tuple[str, str]:
    """Synology / general SMB shares: the user may type the bare share
    name ('backups') or a path-style ('backups/jackery'). Only the first
    segment is the share; anything after is a sub-path inside it which
    we fold into smbclient's `cd` before the actual command.
    """
    share_clean = (share_field or "").lstrip("/")
    if not share_clean:
        return "", ""
    parts = share_clean.split("/")
    return parts[0], "/".join(parts[1:])


def _smb_run(creds: dict, smb_command: str,
             *, timeout_s: float = 30.0) -> subprocess.CompletedProcess:
    """One smbclient session: connect, auth, run the embedded command(s),
    disconnect. Returns the CompletedProcess; caller decides what counts
    as success. Raises SMBClientError only for system-level failures
    (smbclient missing, timeout). Auth / permission errors are reported
    via the returncode + stderr captured on the returned object.
    """
    host = creds["host"]
    share_name, share_subpath = _split_share(creds.get("share") or "")
    if share_subpath:
        smb_command = f'cd "{share_subpath}"; {smb_command}'
    domain = (creds.get("domain") or "WORKGROUP").strip() or "WORKGROUP"
    cmd = [
        "smbclient", f"//{host}/{share_name}",
        "-U", f"{domain}/{creds['username']}%{creds['password']}",
        "-c", smb_command,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        raise SMBClientError(f"smbclient timed out: {e}") from e
    except FileNotFoundError as e:
        raise SMBClientError(
            "smbclient not installed in container") from e


def _smb_extract_error(r: subprocess.CompletedProcess) -> str:
    """Best-effort extraction of the most-actionable line from smbclient
    output. Falls back to a generic exit-code message if neither stderr
    nor stdout has anything useful."""
    merged = "\n".join(s for s in (r.stderr, r.stdout) if s).strip()
    msg_lines = merged.splitlines()
    for line in msg_lines:
        lower = line.lower()
        if ("NT_STATUS_" in line
                or "session setup failed" in lower
                or ("connection to" in lower and "failed" in lower)):
            return line.strip()
    if msg_lines:
        return msg_lines[0].strip()
    return f"smbclient exited {r.returncode}"


def _smb_check(r: subprocess.CompletedProcess) -> None:
    """Raise SMBClientError if smbclient exited non-zero."""
    if r.returncode == 0:
        return
    raise SMBClientError(_smb_extract_error(r))


def _smb_mkdir(creds: dict, remote_path: str) -> None:
    """mkdir -p equivalent over SMB. smbclient's mkdir is single-level
    and errors if the dir already exists, so we walk segments and
    swallow the 'object name collision' / 'already exists' errors
    from each level."""
    parts = [p for p in remote_path.split("/") if p]
    for i in range(1, len(parts) + 1):
        sub = "/".join(parts[:i])
        r = _smb_run(creds, f'mkdir "{sub}"')
        if r.returncode == 0:
            continue
        merged = ((r.stderr or "") + (r.stdout or "")).lower()
        if ("nt_status_object_name_collision" in merged
                or "already exists" in merged):
            continue
        _smb_check(r)


def _smb_put(creds: dict, local: Path, remote: str,
             *, timeout_s: float = 300.0) -> None:
    """Upload one file to a path relative to the share root."""
    r = _smb_run(creds, f'prompt OFF; put "{local}" "{remote}"',
                 timeout_s=timeout_s)
    _smb_check(r)


def _smb_put_dir(creds: dict, local_dir: Path, remote_dir: str,
                 *, timeout_s: float = 600.0) -> None:
    """Upload every file in `local_dir` into `remote_dir` on the share.
    `remote_dir` must already exist (call _smb_mkdir first). Uses mput
    so the whole snapshot ships in one smbclient session."""
    cmd = (
        f'prompt OFF; recurse OFF; '
        f'lcd "{local_dir}"; cd "{remote_dir}"; mput *'
    )
    r = _smb_run(creds, cmd, timeout_s=timeout_s)
    _smb_check(r)


def _smb_get(creds: dict, remote: str, local: Path,
             *, timeout_s: float = 300.0) -> None:
    """Download a file from the share to a local path."""
    r = _smb_run(creds, f'get "{remote}" "{local}"', timeout_s=timeout_s)
    _smb_check(r)


def _smb_get_text(creds: dict, remote: str) -> str:
    """Pull a remote text file into a string (used for MANIFEST.json)."""
    fd, name = tempfile.mkstemp(prefix="jackery-bak-get-")
    os.close(fd)
    tmp = Path(name)
    try:
        _smb_get(creds, remote, tmp)
        return tmp.read_text()
    finally:
        tmp.unlink(missing_ok=True)


def _smb_delete(creds: dict, remote: str) -> None:
    """Best-effort delete; ignore not-found. Used for probe file cleanup
    and rolling back a half-uploaded snapshot."""
    r = _smb_run(creds, f'del "{remote}"')
    if r.returncode == 0:
        return
    merged = ((r.stderr or "") + (r.stdout or "")).lower()
    if "nt_status_object_name_not_found" in merged:
        return
    log.warning("smb delete %s: %s", remote, _smb_extract_error(r))


# smbclient's `ls` output format. With default settings each entry is
#   <ws>name<ws>flags<ws>size<ws>day mon dd hh:mm:ss yyyy
# where flags is a string of letters from {D, A, H, S, R, N}. The 'D'
# flag marks directories; '.' and '..' are returned and filtered.
_LS_LINE = re.compile(r"^\s+(?P<name>\S+)\s+(?P<flags>[DAHSRN]+)\s+(?P<size>\d+)\s")


def _smb_ls(creds: dict, remote_dir: str) -> list[dict[str, Any]]:
    """List entries in `remote_dir` (relative to share root). Returns
    a list of {name, is_dir, size}. Used to enumerate snapshot dirs
    and to size-check files after upload."""
    cd = f'cd "{remote_dir}"; ls' if remote_dir else "ls"
    r = _smb_run(creds, cd)
    _smb_check(r)
    out: list[dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        m = _LS_LINE.match(line)
        if not m:
            continue
        name = m.group("name")
        if name in (".", ".."):
            continue
        out.append({
            "name": name,
            "is_dir": "D" in m.group("flags"),
            "size": int(m.group("size")),
        })
    return out


def _smb_rmtree(creds: dict, remote_dir: str) -> None:
    """Recursively delete a directory tree on the remote (best-effort).
    Used to clean up a half-uploaded snapshot when the post-upload size
    check fails — leaving a corrupt remote dir would make the next
    list_remote_snapshots show a broken entry."""
    try:
        entries = _smb_ls(creds, remote_dir)
    except SMBClientError:
        # Dir is gone or inaccessible — nothing to clean.
        return
    for e in entries:
        path = f"{remote_dir}/{e['name']}"
        if e["is_dir"]:
            _smb_rmtree(creds, path)
        else:
            _smb_delete(creds, path)
    # Empty dir — rmdir.
    r = _smb_run(creds, f'rd "{remote_dir}"')
    if r.returncode != 0:
        log.warning("smb rmdir %s: %s", remote_dir, _smb_extract_error(r))


def test_connectivity(creds: dict | None = None,
                      *, timeout_s: float = 15.0) -> dict[str, Any]:
    """Open an SMB session, ensure the destination subdir exists, write
    a tiny probe file, read it back, delete it. Returns {ok, latency_ms,
    error?}. Designed to run synchronously from the API handler — quick
    enough that the handler can call it in a thread without ceremony.
    """
    creds = creds or backup_creds.load()
    if not creds:
        return {"ok": False, "error": "no_credentials"}
    started = time.time()
    subdir = (creds.get("subdir") or "").strip("/")
    probe_name = f".jackery-probe-{int(started)}"
    probe_remote = f"{subdir}/{probe_name}" if subdir else probe_name

    try:
        if subdir:
            _smb_mkdir(creds, subdir)
        # Probe write + read-back. We use a tempfile so the probe content
        # is on disk in /tmp, not constructed inline (smbclient `put`
        # takes a path).
        fd, name = tempfile.mkstemp(prefix="jackery-probe-")
        os.close(fd)
        local_in = Path(name)
        try:
            local_in.write_text("ok")
            _smb_put(creds, local_in, probe_remote, timeout_s=timeout_s)
        finally:
            local_in.unlink(missing_ok=True)

        fd, name = tempfile.mkstemp(prefix="jackery-probe-back-")
        os.close(fd)
        local_out = Path(name)
        try:
            _smb_get(creds, probe_remote, local_out, timeout_s=timeout_s)
            content = local_out.read_text()
        finally:
            local_out.unlink(missing_ok=True)

        # Clean up the probe even if the read-back content was wrong —
        # we don't want to leave litter on the NAS.
        _smb_delete(creds, probe_remote)

        if content != "ok":
            return {"ok": False, "error": "probe_roundtrip_failed"}
        return {"ok": True, "latency_ms": int((time.time() - started) * 1000)}
    except SMBClientError as e:
        # Best-effort cleanup of the probe — same prefix the UI saw.
        try:
            _smb_delete(creds, probe_remote)
        except Exception:
            pass
        return {"ok": False, "error": f"smb_failed: {e}"}
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
    """List every snapshot directory inside the user's subdir on the
    remote. Each entry has {dir, ts, iso, total_bytes, files} drawn
    from MANIFEST.json (or marked invalid if the manifest is missing
    or unreadable). One smbclient ls round-trip plus one get per
    snapshot dir to fetch its manifest.
    """
    creds = creds or backup_creds.load()
    if not creds:
        return []
    subdir = (creds.get("subdir") or "").strip("/")
    out: list[dict[str, Any]] = []
    try:
        try:
            entries = _smb_ls(creds, subdir)
        except SMBClientError as e:
            # Subdir doesn't exist yet (first run, never backed up) →
            # treat as empty list, not an error.
            merged = str(e).lower()
            if "nt_status_object_name_not_found" in merged or "no such" in merged:
                return []
            raise
        # Newest first — directory names are timestamp-prefixed
        # (YYYY-MM-DD_HHMMSS) so a reverse-string sort is the right
        # chronological order.
        dir_entries = sorted(
            (e for e in entries if e["is_dir"] and not e["name"].startswith(".")),
            key=lambda e: e["name"],
            reverse=True,
        )
        for entry in dir_entries:
            name = entry["name"]
            manifest_remote = f"{subdir}/{name}/MANIFEST.json" if subdir else f"{name}/MANIFEST.json"
            try:
                manifest_text = _smb_get_text(creds, manifest_remote)
            except SMBClientError as e:
                merged = str(e).lower()
                if "nt_status_object_name_not_found" in merged or "no such" in merged:
                    out.append({"dir": name, "valid": False,
                                "error": "manifest_missing"})
                else:
                    out.append({"dir": name, "valid": False,
                                "error": f"manifest_unreadable: {e}"})
                continue
            try:
                m = json.loads(manifest_text)
                out.append({
                    "dir": name,
                    "valid": True,
                    "ts": m.get("ts"),
                    "iso": m.get("iso"),
                    "total_bytes": m.get("total_bytes"),
                    "files": [f.get("name") for f in (m.get("files") or [])],
                    "include_key": bool(m.get("include_key")),
                })
            except Exception as e:
                out.append({"dir": name, "valid": False,
                            "error": f"manifest_unreadable: {e}"})
    except SMBClientError as e:
        log.warning("list snapshots: smb failed: %s", e)
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

        # Upload via smbclient. Pre-flight verify (above) already caught
        # local corruption; SMB transport handles wire integrity. After
        # upload we cross-check sizes via a remote `ls` so a truncated
        # transfer (rare but possible on a flaky link) is caught before
        # the snapshot is declared a success.
        subdir = (creds.get("subdir") or "").strip("/")
        target_dir = f"{subdir}/{dir_stamp}" if subdir else dir_stamp
        try:
            if subdir:
                _smb_mkdir(creds, subdir)
            _smb_mkdir(creds, target_dir)
            _smb_put_dir(creds, staging_dir, target_dir)
            # Cheap remote sanity check: list the just-uploaded dir,
            # compare each file size against the manifest. Catches
            # truncation / missing files without re-downloading every
            # file to re-hash (which mount.cifs let us do for free
            # but smbclient does not).
            err = _verify_remote_sizes(creds, target_dir, manifest)
            if err:
                # Don't leave a corrupt remote snapshot.
                _smb_rmtree(creds, target_dir)
                raise RuntimeError(f"remote_verify_failed: {err}")
        except SMBClientError as e:
            result = BackupResult(
                ok=False, ts=ts, iso=iso, snapshot_dir=None,
                duration_s=time.time() - started,
                bytes_written=0, files_written=0,
                error=f"smb_failed: {e}",
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

    subdir = (creds.get("subdir") or "").strip("/")
    snapshot_remote = (f"{subdir}/{snapshot_dir_name}"
                       if subdir else snapshot_dir_name)

    # Stage the restore in a temp dir on the local side. We download the
    # whole snapshot, verify checksums against MANIFEST.json, and only
    # THEN swap files into /data — so a partial transfer or corrupted
    # file can't half-overwrite the live state.
    staging_root = Path(tempfile.mkdtemp(prefix="jackery-bak-restore-"))
    try:
        try:
            # Pull the manifest first; if the snapshot doesn't exist
            # we get a clear error here without downloading anything.
            try:
                manifest_text = _smb_get_text(
                    creds, f"{snapshot_remote}/MANIFEST.json")
            except SMBClientError as e:
                merged = str(e).lower()
                if "nt_status_object_name_not_found" in merged or "no such" in merged:
                    return {"ok": False,
                            "error": f"snapshot_not_found: {snapshot_dir_name}"}
                raise
            manifest = json.loads(manifest_text)
            (staging_root / "MANIFEST.json").write_text(manifest_text)

            # Decide which files we actually need to fetch.
            files_to_fetch: list[str] = []
            for entry in (manifest.get("files") or []):
                name = entry.get("name")
                if not name:
                    continue
                if files_filter is not None and name not in files_filter:
                    continue
                if name == KEY_FILE and not (scope and scope.get("include_key")):
                    continue
                files_to_fetch.append(name)

            for name in files_to_fetch:
                _smb_get(creds, f"{snapshot_remote}/{name}",
                         staging_root / name)

            # verify_manifest only checks files that physically exist
            # in the staging dir, but it errors on "file missing in
            # snapshot" when a manifest entry has no on-disk file. To
            # support selective restore we filter the manifest to just
            # the files we fetched before verifying.
            staged_manifest = dict(manifest)
            staged_manifest["files"] = [
                f for f in (manifest.get("files") or [])
                if f.get("name") in set(files_to_fetch)
            ]
            (staging_root / "MANIFEST.json").write_text(
                json.dumps(staged_manifest, indent=2, sort_keys=True))
            ok, err = verify_manifest(staging_root)
            if not ok:
                return {"ok": False, "error": f"verify_failed: {err}"}
        except SMBClientError as e:
            return {"ok": False, "error": f"smb_failed: {e}"}

        # Restore to /data via swap-and-replace.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        for name in files_to_fetch:
            src = staging_root / name
            dst = DATA_DIR / name
            tmp = dst.with_suffix(dst.suffix + ".restoring")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
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
    except Exception as e:
        log.exception("restore failed")
        return {"ok": False, "error": str(e)}
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


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
