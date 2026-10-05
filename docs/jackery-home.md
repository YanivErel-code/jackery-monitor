# Jackery Home monitoring (EU)

HomePower 2000 Ultra uses the **Jackery Home** app and a separate cloud API.
A successful login to the portable Jackery App API does not imply that Home
systems appear in its `/v1/device/bind/list` response (issue #2).

In dashboard sign-in, select **Jackery Home (EU — monitoring only)**. This
explicitly selects the Home backend; existing accounts default to Jackery App.
For environment-managed accounts, set `JACKERY_API_FAMILY=home` and
`JACKERY_REGION=EU` alongside the usual credentials. No US Home host has been
verified, so other Home regions are rejected. Never paste credentials or
access tokens into GitHub issues.

## Supported data

- Lists every Home system on the account and lets each browser select a system.
- Shows system SOC and API-reported installed capacity (kWh converted to Wh).
  These values already include installed battery modules; expansions are not
  counted again. Missing or invalid values remain unknown.
- Shows distinct solar, grid, household, AC socket and AC main power fields
  where reported. Signed values are preserved. These are different electrical
  boundaries from portable battery input/output.
- Shows the reported battery-module count, including the main module. Only
  identifiable expansion modules enter the inventory. Individual module SOC,
  temperature and battery power are unavailable through this verified REST
  contract.

This path is **read-only**. It does not subscribe to portable MQTT or send
output commands. Portable charging rules, Kasa rescue, AC recovery, energy
integration and SOC forecasts are disabled for Home systems, including saved
configurations. Unknown battery powers are never recorded as zero-W samples.

The displayed cloud response timestamp is the local HTTP receipt time, **not**
a hardware measurement timestamp. Some REST power fields can remain unchanged
while MQTT has newer readings; the dashboard does not use them for energy or
prediction calculations. Inventory caches expire after two minutes and a
manual refresh requests the Home monitor endpoint again.

## Protocol evidence and validation

The implementation is based on the MIT-licensed
[iLLixM Home integration at e17099a](https://github.com/iLLixM/jackery_home_cloud-ha/tree/e17099aa2a11f6497bfef42eb0fc002754b5f348).
Its documented EU base is
`https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway`.
Verified reference routes used here are:

1. `POST /geneverse-iot-home/v1/home/auth/login` with Home app headers and payload.
2. `GET /geneverse-iot-home/v1/system/listByUserV2` for account systems.
3. `POST /geneverse-iot-home/v1/app/monitor/` with the selected `systemId`.

Responses require a successful envelope and a correctly typed `result`.
Authentication errors enter the bridge's existing session cooldown; errors do
not echo server messages, tokens or passwords. HTTPS certificate verification
is enabled. Tests use synthetic protocol fixtures and make no device commands.
The Node-RED Home flow also found during research derives from this integration;
it is not independent confirmation of the protocol.

**No HomePower 2000 Ultra hardware was available for validation.** Issue #2
remains open until its reporter confirms discovery and readings against the
Jackery Home app. The reporter should select Home/EU, verify the system list,
compare system SOC/capacity and named powers, and share only redacted diagnostics
if the API shape differs. Home does not use the speculative portable cloud
probe endpoint.
