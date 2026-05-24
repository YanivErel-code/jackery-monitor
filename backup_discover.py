"""
NAS auto-discovery for the backup setup wizard.

Two capabilities:

  * discover_smb_hosts() — scan the container's local /24 for hosts
    answering on TCP 445 (SMB). Reverse-resolves names so the UI can
    show "Synology-DS220 · 192.168.1.42" instead of bare IPs. No
    privileges required, no broadcast, runs in plain bridge networking.

  * list_shares(host, username, password, domain) — call `smbclient -L`
    on a host to enumerate the share names available to those creds.
    The UI uses this to turn the share field into a dropdown after
    creds are entered (mirrors macOS Finder's "show available drives"
    UX).

Both functions are synchronous and intended to be called from API
handlers via asyncio.to_thread(). They have aggressive timeouts so a
slow/unreachable host can't wedge the UI.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import os
import re
import socket
import subprocess
import tempfile
from typing import Any

log = logging.getLogger("backup.discover")

# How long to wait for a single TCP connect to port 445 during the
# subnet sweep. Most LAN devices answer in <50ms; this is generous.
_TCP_PROBE_TIMEOUT_S = 0.6

# How wide we'll fan out the subnet scan. With 254 candidates and
# 0.6s per timeout, sequential scan would be ~150s; with 64 workers
# we stay under 5s in the worst case.
_SCAN_WORKERS = 64

# How long to wait for `smbclient -L` to return a share list.
_SMBCLIENT_TIMEOUT_S = 8.0


# ---- subnet inference -----------------------------------------------------


def _local_ipv4() -> str | None:
    """Best-effort: ask the kernel which source IP it would use to
    reach a public address. Doesn't actually send a packet (UDP
    connect is stateless). Returns None if we can't determine it.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 8.8.8.8 is just a routing hint; no traffic leaves.
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _candidate_subnets(local_ip: str | None) -> list[ipaddress.IPv4Network]:
    """Return /24s to sweep. Prefers the container's own subnet, but
    if the container is on a docker bridge (172.17/16 etc.) that
    *isn't* the LAN, we'd return that bridge — useless. So we also
    accept an explicit override via env var BACKUP_SCAN_SUBNETS
    (comma-separated CIDRs).
    """
    import os
    env_override = os.environ.get("BACKUP_SCAN_SUBNETS", "").strip()
    if env_override:
        nets: list[ipaddress.IPv4Network] = []
        for token in env_override.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                nets.append(ipaddress.IPv4Network(token, strict=False))
            except ValueError:
                log.warning("ignoring invalid BACKUP_SCAN_SUBNETS entry: %s", token)
        if nets:
            return nets

    if not local_ip:
        return []
    try:
        # Treat the local IP as if it's on a /24. This is right ~99%
        # of home networks. If the user has a /16 we'll miss most of
        # it, but they can set BACKUP_SCAN_SUBNETS.
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    except ValueError:
        return []
    return [net]


# ---- TCP 445 sweep --------------------------------------------------------


def _probe_445(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(_TCP_PROBE_TIMEOUT_S)
    try:
        s.connect((ip, 445))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _reverse_dns(ip: str) -> str | None:
    """Return a friendly hostname for an IP, or None. Uses the
    standard PTR lookup which works for most home routers / NAS
    devices. Bounded by the system resolver timeout (a few seconds);
    we run these in parallel so total wait is small.
    """
    try:
        name, _aliases, _addrs = socket.gethostbyaddr(ip)
    except (socket.herror, socket.gaierror, OSError):
        return None
    # Trim a trailing .local / .lan / domain — UI shows the
    # short, recognisable label.
    short = name.split(".")[0]
    return short or name


def discover_smb_hosts(*, max_results: int = 32) -> list[dict[str, Any]]:
    """Sweep the container's /24 for SMB hosts. Returns a list of
        [{ip, name, port}]
    sorted with named hosts first, then by IP.

    Designed to be called from an asyncio handler via to_thread().
    Worst-case latency is ~5s on a /24.
    """
    local = _local_ipv4()
    nets = _candidate_subnets(local)
    if not nets:
        log.info("discover_smb_hosts: no candidate subnets (local_ip=%r)", local)
        return []

    candidates: list[str] = []
    for net in nets:
        # Skip network/broadcast and (where possible) our own IP —
        # we're not an SMB server.
        for host in net.hosts():
            ip = str(host)
            if ip == local:
                continue
            candidates.append(ip)

    # Cap to avoid pathological /16 inputs.
    candidates = candidates[: 4096]

    found: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as ex:
        # ex.map yields one result per input in order, so zip with
        # `candidates` is guaranteed length-aligned by construction.
        # `strict=True` would be the textbook safety guard but it's
        # PEP 654 (Python 3.10+) and breaks local test runs on 3.9.
        for ip, ok in zip(candidates, ex.map(_probe_445, candidates)):  # noqa: B905
            if ok:
                found.append(ip)
                if len(found) >= max_results:
                    break

    # Reverse-DNS the survivors in parallel — much faster than serial
    # for 5-10 hosts.
    results: list[dict[str, Any]] = []
    if found:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(found))) as ex:
            names = list(ex.map(_reverse_dns, found))
        # `names` built from list(ex.map(..., found)) — same length as
        # `found` by construction. Same 3.9-compat reason as above.
        for ip, name in zip(found, names):  # noqa: B905
            results.append({"ip": ip, "name": name or ip, "port": 445})

    # Named hosts before bare IPs; otherwise IP-numeric sort.
    def sort_key(h: dict[str, Any]) -> tuple[int, tuple[int, ...]]:
        has_name = 0 if (h.get("name") and h["name"] != h["ip"]) else 1
        try:
            octets = tuple(int(p) for p in h["ip"].split("."))
        except ValueError:
            octets = (0, 0, 0, 0)
        return (has_name, octets)

    results.sort(key=sort_key)
    return results


# ---- share enumeration ----------------------------------------------------


# Examples we need to parse from `smbclient -L`:
#   Sharename       Type      Comment
#   ---------       ----      -------
#   homes           Disk
#   video           Disk
#   IPC$            IPC       IPC Service
_SHARE_LINE = re.compile(
    r"^\s*(?P<name>[^\s]+)\s+(?P<type>Disk|IPC|Printer)\b",
    re.IGNORECASE,
)

# Synology / Samba expose a few admin shares we don't want to show in
# the picker — they're never useful as a backup destination.
_HIDDEN_SHARES = {"IPC$", "ADMIN$", "print$", "NETLOGON", "SYSVOL"}


def _write_smb_authfile(username: str, password: str, domain: str) -> str:
    # smbclient -A reads `username = ... / password = ... / domain = ...`
    # so the password never lands in argv. Duplicated (intentionally) in
    # backup.py — keeping this module free of the backup.py dep weight.
    fd, path = tempfile.mkstemp(prefix="jackery-smbauth-", text=True)
    try:
        os.write(fd, (
            f"username = {username}\n"
            f"password = {password}\n"
            f"domain = {domain}\n"
        ).encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def list_shares(host: str, username: str, password: str,
                *, domain: str = "WORKGROUP",
                timeout_s: float = _SMBCLIENT_TIMEOUT_S) -> dict[str, Any]:
    """Return {ok, shares: [str]} on success or {ok: False, error: ...}.

    Uses `smbclient -L //host -U domain/username%password -g` (machine-
    readable grep mode). Works for Synology, Samba, Windows. Requires
    smbclient to be installed in the container.
    """
    if not host or not username or password is None:
        return {"ok": False, "error": "host, username, password are required"}

    # `-g` switches smbclient to script-friendly output:
    #   Disk|sharename|comment
    #   Disk|video|
    #   IPC|IPC$|IPC Service
    #
    # Important: do NOT pass `-d 0`. It silences smbclient entirely,
    # including the NT_STATUS_* lines that go to stderr on auth /
    # protocol failures — leaving the user staring at a generic
    # "smbclient exited 1" with no actionable detail. The default
    # debug level is fine; we capture and parse only the relevant
    # lines below, the rest get filtered out.
    # Authenticated path uses an authfile so the password never lands
    # in argv (visible via /proc/<pid>/cmdline or `ps`). Guest mode
    # has no password so argv is fine.
    authfile: str | None = None
    if password:
        authfile = _write_smb_authfile(username, password, domain)
        cmd = [
            "smbclient", "-L", f"//{host}",
            "-A", authfile,
            "-g",
        ]
    else:
        # Guest / anonymous mode. -N tells smbclient to skip the
        # password prompt; -U still needs to carry the user/domain.
        cmd = [
            "smbclient", "-L", f"//{host}",
            "-U", f"{domain}/{username}",
            "-g", "-N",
        ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"smbclient timed out after {timeout_s:.0f}s"}
    except FileNotFoundError:
        return {"ok": False,
                "error": "smbclient not installed in container"}
    finally:
        if authfile:
            try:
                os.unlink(authfile)
            except FileNotFoundError:
                pass

    if r.returncode != 0:
        # smbclient writes the most useful diagnostic to stderr (auth
        # failures, protocol negotiation failures, connection refused).
        # Merge stderr + stdout so we never silently lose the line that
        # explains what actually went wrong.
        merged = "\n".join(s for s in (r.stderr, r.stdout) if s).strip()
        log.warning("smbclient -L //%s exited %d:\n%s",
                    host, r.returncode, merged or "(no output)")
        msg_lines = merged.splitlines()
        # Trim noise; pick the first line that mentions an SMB error
        # code or "session setup failed" / "Connection ... failed",
        # else the first non-empty line.
        chosen = ""
        for line in msg_lines:
            lower = line.lower()
            if ("NT_STATUS_" in line
                    or "session setup failed" in lower
                    or ("connection to" in lower and "failed" in lower)):
                chosen = line.strip()
                break
        if not chosen and msg_lines:
            chosen = msg_lines[0].strip()
        return {"ok": False,
                "error": chosen or f"smbclient exited {r.returncode}"}

    shares: list[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        # Format: "Disk|sharename|comment"
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        kind = parts[0].strip()
        name = parts[1].strip()
        if kind.lower() != "disk":
            continue
        if name in _HIDDEN_SHARES:
            continue
        if name and name not in shares:
            shares.append(name)

    # Some smbclient builds don't honour -g and fall back to the
    # human-readable table. Parse that as a fallback.
    if not shares:
        for line in (r.stdout or "").splitlines():
            m = _SHARE_LINE.match(line)
            if not m:
                continue
            if m.group("type").lower() != "disk":
                continue
            name = m.group("name")
            if name in _HIDDEN_SHARES:
                continue
            if name not in shares:
                shares.append(name)

    return {"ok": True, "shares": shares}
