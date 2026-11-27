import forecast_drift

START = 1_800_000_000 // 3600 * 3600
CAPACITY = 30000


def snapshot(predicted_end=40.0, *, solar_w=0.0, output_w=500.0):
    points = [{"ts": START + (hour + 1) * 3600, "duration_h": 1.0, "solar_w": solar_w,
               "output_w": output_w,
               "predicted_soc": 60.0 + (predicted_end - 60.0) * (hour + 1) / 23}
              for hour in range(23)]
    return {"capacity_wh": CAPACITY, "baseline_forecast": points,
            "model": {"charge_efficiency": 0.9, "inverter_overhead_pct": 0.1}}


def hours(*, output_wh=500.0, ac_wh=0.0, car_frac=0.0, solar_wh=0.0):
    return [{"ts": START + hour * 3600, "output_wh": output_wh, "ac_input_wh": ac_wh,
             "solar_wh": solar_wh, "solar_charge_plug_on_frac": car_frac} for hour in range(23)]


def error(actual_end, **kwargs):
    rows = hours(**kwargs.pop("history", {}))
    return forecast_drift.day_error(
        snapshot(**kwargs), made_at=START, start_ts=START, start_soc=60.0, hours=rows,
        soc_at=lambda ts: 60.0 + (actual_end - 60.0) * (ts - START) / (23 * 3600), car_load_w=1300.0)


def test_error_is_forecast_minus_measured_at_both_checks():
    day = error(50.0)
    assert round(day["error_next"], 1) == -10.0
    assert round(day["error_day"], 1) == round(-10.0 * 9 / 23, 1)
    assert round(day["home_kwh"], 1) == 0.0


def test_grid_charging_is_removed_before_comparing():
    plain = error(50.0)["error_next"]
    charged = error(50.0, history={"ac_wh": 400.0})["error_next"]
    gained = 23 * (400 * 0.9 + 500 * 1.1) / CAPACITY * 100
    assert round(charged - plain, 1) == round(gained, 1)


def test_car_charging_is_added_back_and_not_counted_as_home_use():
    day = error(40.0, history={"output_wh": 1800.0, "car_frac": 1.0})
    assert round(day["home_kwh"], 1) == 0.0
    assert round(day["error_next"], 1) == round(-23 * 1300 * 1.1 / CAPACITY * 100, 1)


def test_missing_history_gives_no_record():
    rows = hours()[:-1]
    assert forecast_drift.day_error(snapshot(), made_at=START, start_ts=START, start_soc=60.0,
                                    hours=rows, soc_at=lambda ts: 50.0, car_load_w=1300.0) is None


def day(index, next_error, *, solar=0.0, home=0.0):
    return {"start_ts": START + index * 86400, "error_day": next_error / 2, "error_next": next_error,
            "solar_points": solar, "home_points": home, "solar_kwh": solar / 3, "home_kwh": home / 3}


def test_too_few_days_is_still_learning():
    assert forecast_drift.assess([day(i, 20) for i in range(4)])["status"] == "learning"


def test_small_or_mixed_errors_are_not_drift():
    assert forecast_drift.assess([day(i, 5) for i in range(7)])["status"] == "ok"
    mixed = [day(i, 30 if i < 4 else -5) for i in range(7)]
    assert forecast_drift.assess(mixed)["status"] == "ok"


def test_sustained_error_names_direction_and_cause():
    high = forecast_drift.assess([day(i, 12, home=10) for i in range(7)])
    assert (high["status"], high["direction"], high["cause"]) == ("drifting", "high", "home")
    assert "too high" in high["message"] and "Home use ran" in high["message"]
    low = forecast_drift.assess([day(i, -15, solar=-1, home=-1) for i in range(7)])
    assert (low["status"], low["direction"], low["cause"]) == ("drifting", "low", "model")


def test_only_the_last_week_counts():
    days = [day(i, -20) for i in range(7)] + [day(7 + i, 1) for i in range(7)]
    assert forecast_drift.assess(days)["status"] == "ok"
