"""Tests for backup_discover.py — subnet sweep + share enumeration.

Real network probes and `smbclient` calls are mocked so these run
hermetically in CI.
"""
from __future__ import annotations

import subprocess

import backup_discover

# ---- subnet inference ----------------------------------------------------


def test_candidate_subnets_returns_24_around_local_ip():
    nets = backup_discover._candidate_subnets("192.168.1.42")
    assert len(nets) == 1
    assert nets[0].network_address.exploded == "192.168.1.0"
    assert nets[0].prefixlen == 24


def test_candidate_subnets_empty_when_no_local_ip():
    assert backup_discover._candidate_subnets(None) == []


def test_candidate_subnets_env_override(monkeypatch):
    monkeypatch.setenv("BACKUP_SCAN_SUBNETS", "10.0.0.0/24, 192.168.5.0/24")
    nets = backup_discover._candidate_subnets("172.17.0.2")
    cidrs = sorted(n.with_prefixlen for n in nets)
    assert cidrs == ["10.0.0.0/24", "192.168.5.0/24"]


def test_candidate_subnets_env_override_ignores_garbage(monkeypatch):
    monkeypatch.setenv("BACKUP_SCAN_SUBNETS", "not-a-cidr, 10.0.0.0/24")
    nets = backup_discover._candidate_subnets("10.0.0.5")
    assert [n.with_prefixlen for n in nets] == ["10.0.0.0/24"]


# ---- discover_smb_hosts --------------------------------------------------


def test_discover_returns_empty_when_no_subnet(monkeypatch):
    monkeypatch.setattr(backup_discover, "_local_ipv4", lambda: None)
    monkeypatch.delenv("BACKUP_SCAN_SUBNETS", raising=False)
    assert backup_discover.discover_smb_hosts() == []


def test_discover_finds_responsive_hosts_and_resolves_names(monkeypatch):
    """Two IPs answer on 445; one resolves to a friendly name, the other
    doesn't. Output should be sorted with the named host first."""
    monkeypatch.setattr(backup_discover, "_local_ipv4", lambda: "192.168.1.42")
    monkeypatch.delenv("BACKUP_SCAN_SUBNETS", raising=False)

    responsive = {"192.168.1.10", "192.168.1.99"}
    monkeypatch.setattr(
        backup_discover, "_probe_445", lambda ip: ip in responsive,
    )

    def fake_dns(ip):
        return {"192.168.1.99": "Synology-DS220"}.get(ip)

    monkeypatch.setattr(backup_discover, "_reverse_dns", fake_dns)

    hosts = backup_discover.discover_smb_hosts()
    assert [h["ip"] for h in hosts] == ["192.168.1.99", "192.168.1.10"]
    assert hosts[0]["name"] == "Synology-DS220"
    # Bare IP fallback for the unresolved host
    assert hosts[1]["name"] == "192.168.1.10"
    assert all(h["port"] == 445 for h in hosts)


def test_discover_respects_max_results(monkeypatch):
    monkeypatch.setattr(backup_discover, "_local_ipv4", lambda: "192.168.1.42")
    monkeypatch.delenv("BACKUP_SCAN_SUBNETS", raising=False)
    # Every IP "answers" — we should still cap.
    monkeypatch.setattr(backup_discover, "_probe_445", lambda ip: True)
    monkeypatch.setattr(backup_discover, "_reverse_dns", lambda ip: None)
    hosts = backup_discover.discover_smb_hosts(max_results=3)
    assert len(hosts) == 3


# ---- list_shares ---------------------------------------------------------


def _completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr,
    )


def test_list_shares_parses_grep_format(monkeypatch):
    """smbclient -L -g produces 'Disk|sharename|comment' lines that we
    parse, dropping IPC$ and other admin shares."""
    grep_out = (
        "Disk|video|\n"
        "Disk|backups|primary backup target\n"
        "IPC|IPC$|IPC Service\n"
        "Disk|homes|\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: _completed(stdout=grep_out),
    )
    res = backup_discover.list_shares("nas.local", "alice", "pw")
    assert res["ok"] is True
    assert res["shares"] == ["video", "backups", "homes"]


def test_list_shares_falls_back_to_table_format(monkeypatch):
    """If -g isn't honoured, smbclient prints a Sharename table; we
    parse that as a fallback."""
    table = (
        "        Sharename       Type      Comment\n"
        "        ---------       ----      -------\n"
        "        video           Disk\n"
        "        backups         Disk      primary backup target\n"
        "        IPC$            IPC       IPC Service\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: _completed(stdout=table),
    )
    res = backup_discover.list_shares("nas.local", "alice", "pw")
    assert res["ok"] is True
    assert res["shares"] == ["video", "backups"]


def test_list_shares_surfaces_auth_error(monkeypatch):
    err = (
        "session setup failed: NT_STATUS_LOGON_FAILURE\n"
        "tree connect failed: NT_STATUS_LOGON_FAILURE\n"
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stderr=err, code=1),
    )
    res = backup_discover.list_shares("nas.local", "alice", "wrong")
    assert res["ok"] is False
    assert "NT_STATUS_LOGON_FAILURE" in res["error"]


def test_list_shares_handles_missing_smbclient(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("smbclient")
    monkeypatch.setattr(subprocess, "run", boom)
    res = backup_discover.list_shares("nas.local", "alice", "pw")
    assert res["ok"] is False
    assert "smbclient not installed" in res["error"]


def test_list_shares_handles_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="smbclient", timeout=8)
    monkeypatch.setattr(subprocess, "run", boom)
    res = backup_discover.list_shares("nas.local", "alice", "pw",
                                      timeout_s=8)
    assert res["ok"] is False
    assert "timed out" in res["error"].lower()


def test_list_shares_rejects_missing_inputs():
    res = backup_discover.list_shares("", "alice", "pw")
    assert res["ok"] is False
    res = backup_discover.list_shares("nas.local", "", "pw")
    assert res["ok"] is False
    # Password=None is treated as missing; empty string is allowed for guest.
    res = backup_discover.list_shares("nas.local", "alice", None)
    assert res["ok"] is False
