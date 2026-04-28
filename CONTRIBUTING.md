# Contributing

Thanks for considering a contribution. This is a small personal-scale project
but PRs and issues are welcome.

## Quick start for a dev environment

```bash
# Clone
git clone https://github.com/YanivErel-code/jackery-monitor.git
cd jackery-monitor

# Run in mock mode — no Jackery hardware or cloud needed
docker compose -f docker-compose.dev.yml --profile mock up

# Open http://localhost:8000
```

## Running tests

```bash
pip install -r requirements.txt
pip install pytest pytest-asyncio
pytest
```

## Code style

- Python: type hints on public APIs, docstrings on non-trivial functions, no
  `print()` in production paths (use the `logging` module).
- JS: vanilla, no build step, no framework. Keep it readable; no minification.
- CSS: dark theme variables in `:root`. Add new colors as variables, not
  hex literals scattered through.
- Single source files for the dashboard (`web/style.css`, `web/app.js`) — don't
  introduce a build pipeline without a strong reason.

## What kinds of changes are welcome

- Bug fixes (especially anything around the reverse-engineered cloud
  protocol — they shift)
- Tests for under-covered modules (see `tests/` for current coverage)
- New automation actions beyond Kasa (e.g. Tasmota, Shelly, MQTT-publish)
- New chart features, mobile-layout fixes, accessibility improvements
- Better docs / troubleshooting entries from your own stumbles

## What I'm probably not going to merge

- Adding a heavy frontend framework (React/Vue/etc.) — see the no-build-step
  rule above
- Multi-tenant / multi-user features — this is a personal monitor
- Anything that introduces a paid third-party dependency

## How to report a security issue

See [SECURITY.md](./SECURITY.md). Don't open a public issue for security bugs.

## How to open a useful issue

- Tell me what you tried and what happened (bridge log lines help a lot).
- For automation problems, the **Logs tab** in the dashboard is the fastest
  way to see what's happening.
- For Kasa issues, mention the device model — the protocol differs across
  generations.
