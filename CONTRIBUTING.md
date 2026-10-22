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
pip install -r requirements-dev.txt   # pytest + pytest-asyncio + lint tools
pytest
```

`requirements-dev.txt` is the source of truth for the test toolchain —
don't `pip install pytest` ad-hoc. The async test suite needs
`pytest-asyncio` (the project sets `asyncio_mode = "auto"` in
`pyproject.toml`); without it, every `async def` test fails with
*"async def functions are not natively supported"* even though the
code under test is fine.

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

## Cutting a release

Release metadata and upgrade notes go through a PR first. Review the commits and
tree diff from the previous release, update `CHANGELOG.md`, and add the version's
guide under `docs/releases/`. Keep operational-policy changes and unsupported
hardware limitations explicit; do not describe a major version as a safety or
forecast-accuracy guarantee.

After the release PR is merged, require both CI and the Docker build to succeed
for that exact main commit. Cut the GitHub release from that reviewed SHA using
the GitHub CLI and the committed notes, not a hand-created or moved local tag:

```bash
gh release create v1.0.0 --repo YanivErel-code/jackery-monitor \
  --target <reviewed-main-commit> --title 'v1.0.0 — Forecasting, recovery and appliance-aware charging' \
  --notes-file docs/releases/v1.0.0.md --latest
```

Use the next approved version and corresponding guide for future releases.
The existing tag-triggered workflow publishes the versioned container image.
Wait for that build, verify its revision/digest, and attach the generated SBOM,
provenance and a checksum manifest to the GitHub release. BuildKit stores these
attestations with the image; they are build evidence, not a signed security audit.
Never overwrite an existing release tag or include runtime data/credentials in
release assets. Synology deployment is a separate, explicit operation.

## How to open a useful issue

- Tell me what you tried and what happened (bridge log lines help a lot).
- For automation problems, the **Logs tab** in the dashboard is the fastest
  way to see what's happening.
- For Kasa issues, mention the device model — the protocol differs across
  generations.
