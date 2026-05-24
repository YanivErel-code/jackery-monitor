# Security policy

## Reporting a vulnerability

Please **don't** open a public GitHub issue for security bugs. Instead,
email me at the address listed on my GitHub profile, or open a
[private security advisory](https://github.com/YanivErel-code/jackery-monitor/security/advisories/new).

I'll acknowledge within a few days. This is a personal-scale project so
"a few days" is realistic; nothing here is mission-critical.

## Threat model — what this app actually protects

### Encrypted at rest (AES-256-GCM, `/data/.jackery-creds.key`)

1. **Jackery cloud account** (`/data/jackery-creds.json`)
2. **Kasa cloud account** (`/data/kasa-creds.json`)
3. **Backup destination credentials** — SMB/rsync/SSH (`/data/backup-creds.json`)
4. **Anthropic API key** for the optional Claude features (`/data/anthropic-creds.json`)
5. **Dashboard login** — username + PBKDF2-SHA256 hash (`/data/auth.json`)
6. **Device location** — lat/lon, label, UTC offset (`/data/location.json`)

All share the same key file at `/data/.jackery-creds.key` (mode 0600),
generated on first use. **The key is co-located with the ciphertext** —
encryption-at-rest here protects against image leaks and casual
filesystem access, NOT against full root compromise of the host.

Dashboard password hashing uses PBKDF2-SHA256 with 600,000 iterations
(OWASP 2023 minimum). The iteration count is embedded per-hash so
older hashes keep verifying with their original count after bumps.

### Not encrypted at rest (by design)

- **`/data/energy.db`** — SQLite database holding device serials, SOC
  history, weather observations, automation firings. Encrypting a live
  SQLite DB would require SQLCipher or per-query wrapping — invasive
  changes that we don't justify for the documented threat model
  (single-user, behind LAN/Cloudflare). Backup snapshots include this
  DB *as-is*; they inherit the privacy posture of the destination share.
  If you back up to a destination you don't fully trust, layer your
  own encryption on the share itself (e.g. SMB-over-WireGuard, an
  encrypted volume on the NAS).
- **`/data/settings.json`, `/data/automation.json`, `/data/kasa_devices.json`,
  `/data/smart_charge.json`, `/data/anthropic-prefs.json`,
  `/data/cost.json`, `/data/tunables.json`** — application config
  and non-sensitive operational state. No credentials, no PII beyond
  device serial numbers (semi-PII per GDPR but visible on the device
  label).

### Sub-process secrets handling

- SMB passwords are passed to `smbclient` via `-A <authfile>` written
  to a 0600 tempfile (cleaned up in `finally`), **never argv**, so
  they don't appear in `ps aux` / `/proc/<pid>/cmdline`.
- SSH rsync key material is written to a 0600 tempfile passed via
  `-e "ssh -i …"` and unlinked after the rsync invocation.
- rsyncd/SSH passwords ride in `$RSYNC_PASSWORD` / `$SSHPASS` env
  vars scoped to the child process only.

### Cookie / session hardening

- Session cookies are HttpOnly + SameSite=Lax. The `Secure` flag is
  off by default because the canonical deployment terminates TLS at
  Cloudflare Tunnel (origin sees plain HTTP). Operators running
  direct HTTPS at the origin should set `JACKERY_COOKIE_SECURE=1`.

## What is in scope

- The dashboard's login flow (PBKDF2 work factor, session HMAC, cookie
  flags)
- The crypto utility (`crypto_util.py`) — AES-256-GCM correctness,
  key handling
- Anything that lets an unauthenticated user reach `/api/*`
- Anything that lets a Jackery cloud response cause RCE / SQL injection
  / path traversal
- Anything that lets a Kasa device response cause RCE / SQL injection /
  path traversal

## What is not in scope

- Vulnerabilities in upstream Jackery cloud / Kasa cloud / their devices
  themselves (please report those to the vendor)
- Rate limiting at the dashboard level — currently none, intentionally
  (single-user app behind Cloudflare Access / LAN)
- Multi-tenant attacks — this is a single-user app
