"""Bounded same-day solar correction from completed, uncurtailed observations."""
from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fit_intraday_solar_scale(
    history: list[dict[str, Any]],
    weather: list[dict[str, Any]],
    coefficient: float,
    diurnal_shape: dict[int, float],
    *,
    now_ts: float,
    utc_offset_seconds: int = 0,
) -> dict[str, Any]:
    """Correct only today's remaining solar, never tomorrow's weather.

    Require two complete daylight hours with fresh SOC receipts, headroom,
    no AC competition and consistently biased observed/model energy. Apply
    half the energy-weighted bias, bounded to +/-20%. Two hours without a
    qualified reading expire the correction. No cloud attenuation is added.
    """
    result = {"scale": 1.0, "source": "base_model", "eligible_hours": 0,
              "reason": "insufficient_qualified_hours"}
    if not _finite(now_ts) or not _finite(coefficient) or coefficient <= 0:
        return result
    day = int((now_ts + utc_offset_seconds) // 86400)
    by_end = {int(w['ts']): w for w in weather
              if _finite(w.get('ts')) and w['ts'] <= now_ts}
    pairs: list[tuple[float, float, int]] = []
    seen: set[int] = set()
    for row in history:
        ts = row.get('ts')
        if not _finite(ts) or ts % 3600 or ts + 3600 > now_ts:
            continue
        stamp = int(ts)
        if (stamp + utc_offset_seconds) // 86400 != day:
            continue
        if stamp in seen:
            return {**result, "reason": "duplicate_history"}
        seen.add(stamp)
        if row.get('history_complete') is not True or row.get('soc_fit_excluded'):
            continue
        known = row.get('soc_source_known_buckets')
        first = row.get('soc_source_ts_min')
        last = row.get('soc_source_ts_max')
        if (not all(_finite(v) for v in (known, first, last)) or known < 55
                or first < ts - 120 or last > ts + 3600):
            continue
        soc = row.get('system_soc', row.get('battery_pct'))
        main_soc = row.get('battery_pct')
        ac = row.get('ac_input_w')
        measured = row.get('solar_wh')
        if not all(_finite(v) for v in (soc, main_soc, ac, measured)):
            continue
        if not (0 <= soc <= 70 and 0 <= main_soc <= 70 and 0 <= ac <= 50 and measured >= 0):
            continue
        conditions = by_end.get(stamp + 3600)
        ghi = conditions.get('ghi_w_m2') if conditions else None
        hour = ((stamp + utc_offset_seconds) % 86400) // 3600
        shape = diurnal_shape.get(hour, 1.0)
        if not _finite(ghi) or ghi < 100 or not _finite(shape) or shape <= 0:
            continue
        predicted = coefficient * ghi * shape
        if predicted < 100 or not 0.4 <= measured / predicted <= 2:
            continue
        pairs.append((measured, predicted, stamp + 3600))
    result['eligible_hours'] = len(pairs)
    if len(pairs) < 2:
        return result
    if now_ts - max(p[2] for p in pairs) > 2 * 3600:
        return {**result, 'reason': 'stale_observations'}
    ratios = [actual / predicted for actual, predicted, _ in pairs]
    if max(ratios) - min(ratios) > 0.4:
        return {**result, 'reason': 'inconsistent_observations'}
    if not (min(ratios) >= 1.08 or max(ratios) <= 0.92):
        return {**result, 'reason': 'no_consistent_material_bias'}
    ratio = sum(p[0] for p in pairs) / sum(p[1] for p in pairs)
    scale = max(0.8, min(1.2, 1 + 0.5 * (ratio - 1)))
    return {**result, 'scale': round(scale, 6), 'source': 'intraday_observations',
            'reason': 'consistent_same_day_bias'}
