"""Direct/diffuse solar model: tilted panels gain far more from direct sun
than from diffuse light, so one coefficient on GHI under-forecasts clear
days and over-forecasts overcast ones (2026-11-23)."""
from __future__ import annotations

import time

import pytest

import forecaster
import weather_client
from solar_calibration import fit_intraday_solar_scale

BASE = 1_790_000_000 // 86400 * 86400
K, RATIO = 4.5, 0.5


def _days(skies, *, split=True, start=BASE):
    """Hourly history + weather for consecutive days of the given skies.
    Panels make K * (direct + RATIO * diffuse)."""
    energy, weather = [], []
    for day, sky in enumerate(skies):
        for hour in range(8, 17):
            ts = start + day * 86400 + hour * 3600
            ghi = 500.0 - 40 * abs(hour - 12)
            direct, diffuse = (0.8 * ghi, 0.2 * ghi) if sky == "clear" else (0.0, 0.5 * ghi)
            row = {"ts": ts + 3600, "ghi_w_m2": direct + diffuse, "cloud_cover_pct": 0 if sky == "clear" else 100}
            if split:
                row.update(direct_w_m2=direct, diffuse_w_m2=diffuse)
            weather.append(row)
            energy.append({"ts": ts, "solar_w": K * (direct + RATIO * diffuse), "battery_pct": 50, "ac_input_w": 0})
    return energy, weather


def test_split_fit_recovers_both_responses_from_mixed_weather():
    energy, weather = _days(["clear", "overcast"] * 7)
    k, ratio, n = forecaster.fit_solar_split(energy, weather)
    assert n == 14 * 9
    assert k == pytest.approx(K, rel=0.03) and ratio == pytest.approx(RATIO, abs=0.05)
    # the single GHI coefficient lands between the two and misses both kinds of day
    k_ghi, _ = forecaster.fit_solar_coefficient(energy, weather)
    clear, overcast = weather[4], weather[13]
    assert k_ghi * clear["ghi_w_m2"] < 0.95 * energy[4]["solar_w"]
    assert k_ghi * overcast["ghi_w_m2"] > 1.5 * energy[13]["solar_w"]
    for w, e in ((clear, energy[4]), (overcast, energy[13])):
        blended = forecaster.with_solar_irradiance([w], ratio)[0]
        # (the prior on the ratio costs a few percent when the data alone would do)
        assert k * forecaster.solar_irradiance(blended) == pytest.approx(e["solar_w"], rel=0.1)


def test_split_fit_needs_enough_split_history():
    energy, weather = _days(["clear", "overcast"] * 7, split=False)
    assert forecaster.fit_solar_split(energy, weather) is None
    energy, weather = _days(["clear", "overcast"])                  # 18 hours
    assert forecaster.fit_solar_split(energy, weather) is None
    assert forecaster.fit_solar_split([], []) is None


def test_only_clear_days_cannot_tell_the_two_apart_so_the_prior_decides():
    energy, weather = _days(["clear"] * 14)
    k, ratio, _ = forecaster.fit_solar_split(energy, weather)
    lo, hi = forecaster.SOLAR_DIFFUSE_RATIO_CLAMP
    assert lo <= ratio <= hi
    # whatever the split, the clear days it learned from are reproduced
    blended = forecaster.with_solar_irradiance(weather, ratio)
    assert k * forecaster.solar_irradiance(blended[4]) == pytest.approx(energy[4]["solar_w"], rel=0.03)


def test_split_fit_prefers_hours_with_headroom():
    energy, weather = _days(["clear", "overcast"] * 7)
    for row in energy[: 9 * 4]:                 # four days curtailed by a full battery
        row.update(battery_pct=98, solar_w=row["solar_w"] * 0.3)
    k, ratio, n = forecaster.fit_solar_split(energy, weather)
    assert n == 10 * 9 and k == pytest.approx(K, rel=0.03)


def test_hours_stored_without_the_split_count_as_no_daylight():
    rows = forecaster.with_solar_irradiance(
        [{"ts": 1, "ghi_w_m2": 400.0}, {"ts": 2, "ghi_w_m2": 400.0, "direct_w_m2": 300.0, "diffuse_w_m2": 100.0}], 0.5)
    assert [forecaster.solar_irradiance(r) for r in rows] == [0.0, 350.0]
    assert forecaster.solar_irradiance({"ghi_w_m2": 400.0}) == 400.0      # untouched rows fall back to GHI


def _forecast(split_ahead=True):
    now = int(time.time()) // 86400 * 86400 + 86400 * 0 + 6 * 3600
    start = now // 86400 * 86400 - 14 * 86400
    energy, weather = _days(["clear", "overcast"] * 7, start=start)
    for row in energy:
        row.update(solar_wh=row["solar_w"], output_w=100, output_wh=100, ac_input_wh=0)
    # night rows so the readiness gate sees discharge windows
    for day in range(14):
        for hour in list(range(0, 8)) + list(range(17, 24)):
            ts = start + day * 86400 + hour * 3600
            energy.append({"ts": ts, "solar_w": 0, "solar_wh": 0, "output_w": 100, "output_wh": 100,
                           "ac_input_w": 0, "ac_input_wh": 0, "battery_pct": 50 - ((hour - 17) % 24)})
            weather.append({"ts": ts + 3600, "ghi_w_m2": 0.0, "cloud_cover_pct": 0, "direct_w_m2": 0.0, "diffuse_w_m2": 0.0})
    energy.sort(key=lambda r: r["ts"])
    _, ahead = _days(["overcast", "clear"], split=split_ahead, start=now // 86400 * 86400)
    return forecaster.build_forecast(
        energy_history=energy, weather_hourly=sorted(weather + ahead, key=lambda w: w["ts"]),
        starting_soc_pct=50.0, capacity_wh=30240, now_ts=now, horizon_hours=48), ahead


def test_build_forecast_uses_the_split_model_when_the_weather_has_it():
    res, ahead = _forecast()
    assert res["ready"], res.get("readiness")
    assert res["solar_model"] == "direct_diffuse"
    assert res["solar_coefficient"] == pytest.approx(K, rel=0.05)
    assert res["solar_diffuse_ratio"] == pytest.approx(RATIO, abs=0.06)
    by_ts = {h["ts"]: h for h in res["forecast"]}
    overcast, clear = ahead[4], ahead[13]
    assert by_ts[overcast["ts"]]["solar_w"] == pytest.approx(K * RATIO * overcast["diffuse_w_m2"], rel=0.1)
    assert by_ts[clear["ts"]]["solar_w"] == pytest.approx(
        K * (clear["direct_w_m2"] + RATIO * clear["diffuse_w_m2"]), rel=0.1)
    assert by_ts[clear["ts"]]["ghi_w_m2"] == clear["ghi_w_m2"]             # still reports the real GHI


def test_build_forecast_stays_on_ghi_when_the_hours_ahead_have_no_split():
    res, ahead = _forecast(split_ahead=False)
    assert res["ready"] and res["solar_model"] == "ghi" and res["solar_diffuse_ratio"] is None
    k_ghi = res["solar_coefficient"]
    by_ts = {h["ts"]: h for h in res["forecast"]}
    assert by_ts[ahead[13]["ts"]]["solar_w"] == pytest.approx(
        k_ghi * ahead[13]["ghi_w_m2"] * res["diurnal_shape"]["12"], rel=0.05)


def test_same_day_correction_compares_against_the_blended_irradiance():
    history, weather = [], []
    for hour in (8, 9):
        ts = BASE + hour * 3600
        history.append({"ts": ts, "history_complete": True, "solar_wh": 1400, "system_soc": 45, "battery_pct": 50,
                        "ac_input_w": 0, "soc_source_known_buckets": 60, "soc_source_ts_min": ts + 30,
                        "soc_source_ts_max": ts + 3570})
        weather.append({"ts": ts + 3600, "ghi_w_m2": 600, "direct_w_m2": 100.0, "diffuse_w_m2": 500.0})
    plain = fit_intraday_solar_scale(history, weather, 4, {}, now_ts=BASE + 11 * 3600)
    assert plain["reason"] == "severe_site_shortfall"                       # 1400 against 4*600
    blended = fit_intraday_solar_scale(history, forecaster.with_solar_irradiance(weather, 0.5), 4, {},
                                       now_ts=BASE + 11 * 3600)
    assert blended["scale"] == 1.0 and blended["reason"] == "no_consistent_material_bias"   # 1400 against 4*350


def test_observations_stored_before_the_split_get_it_filled_in(tmp_path):
    from energy_db import EnergyDB
    db = EnergyDB(str(tmp_path / "energy.db"))
    db.upsert_weather_observations([{"ts": 3600, "ghi_w_m2": 400.0, "cloud_cover_pct": 10.0}])
    assert db.list_weather_observations() == [{"ts": 3600, "ghi_w_m2": 400.0, "cloud_cover_pct": 10.0}]
    # a later fetch carries the split: it is added, the recorded GHI is kept
    db.upsert_weather_observations([{"ts": 3600, "ghi_w_m2": 433.0, "cloud_cover_pct": 50.0,
                                     "direct_w_m2": 300.0, "diffuse_w_m2": 133.0}])
    assert db.list_weather_observations() == [{"ts": 3600, "ghi_w_m2": 400.0, "cloud_cover_pct": 10.0,
                                               "direct_w_m2": 300.0, "diffuse_w_m2": 133.0}]
    # and once stored it is not overwritten either
    db.upsert_weather_observations([{"ts": 3600, "ghi_w_m2": 1.0, "direct_w_m2": 1.0, "diffuse_w_m2": 1.0}])
    assert db.list_weather_observations()[0]["direct_w_m2"] == 300.0
    db.replace_weather_forecast([{"ts": 7200, "ghi_w_m2": 500.0, "cloud_cover_pct": 0.0,
                                  "direct_w_m2": 420.0, "diffuse_w_m2": 80.0},
                                 {"ts": 10800, "ghi_w_m2": 500.0, "cloud_cover_pct": 0.0}], fetched_at=100)
    rows, _ = db.get_weather_forecast()
    assert rows[0]["direct_w_m2"] == 420.0 and "direct_w_m2" not in rows[1]


class _Client:
    def __init__(self, payload):
        self.payload, self.params = payload, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.params = params
        payload = self.payload

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload
        return _Response()


@pytest.mark.asyncio
async def test_weather_fetch_asks_for_and_returns_the_split(monkeypatch):
    weather_client._cache.clear()
    client = _Client({"hourly": {"time": [3600, 7200, 10800], "shortwave_radiation": [400, 300, 200],
                                 "cloud_cover": [0, 100, 50], "direct_radiation": [320, 0, None],
                                 "diffuse_radiation": [80, 300, 90]}})
    monkeypatch.setattr(weather_client.httpx, "AsyncClient", lambda *a, **k: client)
    import energy_db
    monkeypatch.setattr(energy_db.EnergyDB, "upsert_weather_observations", lambda self, rows: 0)
    monkeypatch.setattr(energy_db.EnergyDB, "replace_weather_forecast", lambda self, rows, fetched_at: 0)
    out = await weather_client.fetch_irradiance(37.0, -122.0)
    weather_client._cache.clear()
    assert "direct_radiation" in client.params["hourly"] and "diffuse_radiation" in client.params["hourly"]
    first, second, third = out["hourly"]
    assert (first["direct_w_m2"], first["diffuse_w_m2"]) == (320.0, 80.0)
    assert (second["direct_w_m2"], second["diffuse_w_m2"]) == (0.0, 300.0)
    assert "direct_w_m2" not in third                                       # a missing half means no split


def test_settings_shows_the_values_the_forecast_uses(monkeypatch):
    """What the app has learned: the direct-sun response with the worth of
    diffuse light as its note, or the single GHI coefficient without one."""
    import server
    energy, weather = _days(["clear", "overcast"] * 7)
    saved = {}
    monkeypatch.setattr(server.state.energy, "get_device_param", lambda sn, key: None)
    monkeypatch.setattr(server.state.energy, "set_device_param",
                        lambda sn, key, value, **kw: saved.update(value=value, **kw))
    monkeypatch.setattr(server, "_cached_history", lambda sn: energy)
    monkeypatch.setattr(server.state.energy, "list_weather_observations", lambda **kw: weather)
    resolved = server.resolve_device_param("MAIN", "solar_coefficient")
    assert resolved["value"] == pytest.approx(K, rel=0.03) and resolved["source"] == "fit"
    assert resolved["note"].startswith("Watts per W/m² of direct sun.") and "% of that" in resolved["note"]
    assert saved["note"] == resolved["note"]

    plain = [{k: v for k, v in w.items() if k not in ("direct_w_m2", "diffuse_w_m2")} for w in weather]
    monkeypatch.setattr(server.state.energy, "list_weather_observations", lambda **kw: plain)
    resolved = server.resolve_device_param("MAIN", "solar_coefficient")
    assert resolved["note"] is None and saved["note"] is None
    assert resolved["value"] == pytest.approx(forecaster.fit_solar_coefficient(energy, plain)[0])
