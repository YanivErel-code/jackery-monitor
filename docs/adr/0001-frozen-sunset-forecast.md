# Immutable sunset-to-sunrise accuracy baseline

Date: 2026-10-14
Status: Accepted

## Context

Ready forecasts are recalculated from current SOC, weather and load estimates.
The daily sunrise prediction was overwritten every five minutes and split into
a new row at midnight. Comparing that late revision with actual sunrise SOC hid
the error in the original overnight prediction. Hourly forecast history also
replaces predictions within each creation-hour bucket, so it cannot recover an
exact original after the fact.

## Decision

Keep the existing rolling fields for compatibility, but capture a separate,
immutable baseline in `daily_solar_summary`. The first ready forecast made
between the forecast's sunset boundary and 15 minutes later wins atomically.
This accommodates the five-minute controller/background check without treating
a much later, shorter-horizon prediction as an overnight forecast. Sunset is
the model's last positive-solar hour, not an astronomical sunset calculation.

The new fields are:

| Field | Meaning |
| --- | --- |
| `sunset_forecast_soc_pct` | Original predicted system SOC at sunrise |
| `sunset_forecast_made_at` | Exact snapshot creation time (Unix seconds) |
| `sunset_forecast_sunrise_ts` | Immutable original evaluation target (Unix seconds) |
| `sunset_forecast_actual_soc_pct` | Capacity-weighted actual SOC around that target |
| `sunset_forecast_error_pp` | API-derived actual minus original prediction |

Rows belong to the sunset date, even when updated after local midnight. If
weather later shifts sunrise, the snapshot target and its measured actual stay
unchanged. Finalize that actual only after the existing ±30-minute observation
window has elapsed. Sunrise MAE and hit rate use only original snapshots;
latest rolling estimates are shown separately with their own target/time.

The background recorder still builds normally once an hour. It also checks
every five minutes for devices in an uncaptured sunset window, rebuilding only
those devices. Capturing does not depend on smart charge or dashboard traffic.
No forecasting or hardware-control algorithm is changed.

## Consequences and alternatives

Existing rows and nights without a ready forecast in the capture window have
unknown originals. They remain visible but cannot contribute to sunrise
accuracy. This intentionally rejects retroactive reconstruction from rounded,
overwritten hourly history, and rejects capturing the first arbitrary late
night prediction. Extra model work is bounded to missing sunset captures;
rebuilding every device every five minutes was unnecessary. Timestamps in the
dashboard use the browser's time zone, while row dates use the device location.
