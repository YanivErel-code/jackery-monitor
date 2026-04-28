# Security policy

## Reporting a vulnerability

Please **don't** open a public GitHub issue for security bugs. Instead,
email me at the address listed on my GitHub profile, or open a
[private security advisory](https://github.com/YanivErel-code/jackery-monitor/security/advisories/new).

I'll acknowledge within a few days. This is a personal-scale project so
"a few days" is realistic; nothing here is mission-critical.

## Threat model — what this app actually protects

The Jackery Monitor stores three kinds of credentials:

1. **Jackery cloud account** — encrypted at rest with AES-256-GCM
   (`/data/jackery-creds.json`).
2. **Kasa cloud account** — same scheme (`/data/kasa-creds.json`).
3. **Dashboard login** — username + PBKDF2-SHA256 hash, encrypted at
   rest (`/data/auth.json`).

All three are encrypted with the same key file at
`/data/.jackery-creds.key` (mode 0600), generated on first use. **The
key is co-located with the ciphertext** — encryption-at-rest here
protects against image leaks and casual filesystem access, NOT
against full root compromise of the host.

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
