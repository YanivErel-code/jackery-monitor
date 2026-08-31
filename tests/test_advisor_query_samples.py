"""Tests for the advisor's `query_samples` tool (advisor_routes).

Two shipped defects made the advisor repeatedly (and falsely) report
"sub-hour telemetry is incomplete" against a `samples` table that is in
fact complete at 60s resolution:

  1. The Wh->W fields were hardcoded to None unless bucket_s == 3600, so
     every 5-/15-minute query came back with null power columns.
  2. The lookback handed to energy_db.history() was sized from the
     window's DURATION (end - start), but history() interprets `hours`
     as "back from NOW". Any window that had already scrolled past —
     last night, say — fetched only the most recent few hours and the
     clip loop then discarded every row, yielding an empty result that
     reads exactly like missing data.

These tests pin the averaging math across bucket sizes and keep the
lookback anchored to `start`.
"""
from __future__ import annotations

import calendar
import time

import pytest

import advisor_routes

SN = "TEST-QS"
# Flat synthetic load: a true average of exactly this many watts, whatever
# bucket width the caller asks for. Scale-invariance is the whole point of
# the Wh / (bucket_s/3600) conversion.
CONST_OUT_W = 900
CONST_IN_W = 250
CONST_SOLAR_W = 120
CONST_AC_W = 60

FIXED_NOW = 1_788_000_000  # arbitrary fixed clock so windows are deterministic


class FakeEnergy:
    """Stands in for energy_db.EnergyDB, reproducing the two behaviours
    query_samples has to cope with: `hours` counts back from now, and
    bucket_s is floored at the sampler's own period."""

    SAMPLE_BUCKET_S = 60

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def history(self, device_sn, hours=24, bucket_s=600,
                main_capacity_wh=None, pack_capacity_wh=None):
        self.calls.append({"hours": hours, "bucket_s": bucket_s})
        bucket_s = max(self.SAMPLE_BUCKET_S, int(bucket_s))
        since = FIXED_NOW - hours * 3600
        first = (since // bucket_s) * bucket_s
        last = (FIXED_NOW // bucket_s) * bucket_s
        bucket_h = bucket_s / 3600.0
        return [
            {
                "ts": ts,
                "input_wh": CONST_IN_W * bucket_h,
                "output_wh": CONST_OUT_W * bucket_h,
                "solar_wh": CONST_SOLAR_W * bucket_h,
                "ac_input_wh": CONST_AC_W * bucket_h,
                "input_w": CONST_IN_W,
                "output_w": CONST_OUT_W,
                "solar_w": CONST_SOLAR_W,
                "battery_pct": 50,
            }
            for ts in range(first, last + 1, bucket_s)
        ]


class FakeState:
    def __init__(self) -> None:
        self.energy = FakeEnergy()


@pytest.fixture()
def query(monkeypatch):
    """The real tool dispatcher, wired to a fake DB and a frozen clock."""
    monkeypatch.setattr(advisor_routes.time, "time", lambda: FIXED_NOW)
    state = FakeState()
    helpers = advisor_routes.AdvisorHelpers(
        total_capacity_wh=lambda sn, model=None: 30240,
        capacity_hints=lambda sn: (2160, 2016),
        system_soc_pct=lambda pct, sn, model=None: pct,
    )
    fn = advisor_routes._make_advisor_query_fn(state, helpers, SN)
    fn.state = state  # tests inspect what reached history()
    return fn


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))


# ---- 1. averaging math -------------------------------------------------

@pytest.mark.parametrize("bucket_s", [300, 900, 3600])
async def test_avg_watts_are_true_averages_at_every_bucket(query, bucket_s):
    """average_W = bucket_Wh / (bucket_s/3600), so a flat 900W load reads
    900W whether it's bucketed at 5min, 15min or 1h. Before the fix the
    two sub-hour cases returned None."""
    start, end = FIXED_NOW - 6 * 3600, FIXED_NOW
    res = await query("query_samples", {
        "start_iso": _iso(start), "end_iso": _iso(end), "bucket_s": bucket_s,
    })

    assert res["rows"], f"bucket_s={bucket_s} returned no rows"
    for row in res["rows"]:
        assert row["out_w_avg"] == CONST_OUT_W
        assert row["in_w_avg"] == CONST_IN_W
        assert row["solar_w_avg"] == CONST_SOLAR_W
        assert row["ac_input_w_avg"] == CONST_AC_W


async def test_avg_watts_agree_across_bucket_sizes(query):
    """The same underlying energy, re-bucketed, must integrate to the same
    mean power — the invariant the old None-unless-hourly branch made
    impossible to check."""
    start, end = FIXED_NOW - 6 * 3600, FIXED_NOW
    means = {}
    for bucket_s in (300, 900, 3600):
        res = await query("query_samples", {
            "start_iso": _iso(start), "end_iso": _iso(end),
            "bucket_s": bucket_s,
        })
        rows = res["rows"]
        means[bucket_s] = sum(r["out_w_avg"] for r in rows) / len(rows)
    assert means[300] == means[900] == means[3600] == CONST_OUT_W


async def test_hourly_behaviour_is_numerically_unchanged(query):
    """At bucket_s=3600 the divisor is 1, so the reported watts must still
    equal int(bucket_Wh) exactly — the pre-fix hourly contract."""
    start, end = FIXED_NOW - 6 * 3600, FIXED_NOW
    res = await query("query_samples", {
        "start_iso": _iso(start), "end_iso": _iso(end), "bucket_s": 3600,
    })
    raw = query.state.energy.history(SN, hours=7, bucket_s=3600)
    by_ts = {r["ts"]: r for r in raw}
    assert res["rows"]
    for row in res["rows"]:
        ts = calendar.timegm(time.strptime(row["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
        assert row["out_w_avg"] == int(by_ts[ts]["output_wh"])


# ---- 2. the "no rows overnight" regression -----------------------------

async def test_past_window_returns_rows(query):
    """REGRESSION: a 6h window that ended ~10h ago (last night) used to
    size the lookback at 7h, so history() never reached it and the clip
    dropped everything. The lookback must span now->start."""
    start = FIXED_NOW - 16 * 3600
    end = start + 6 * 3600
    res = await query("query_samples", {
        "start_iso": _iso(start), "end_iso": _iso(end), "bucket_s": 300,
    })

    assert res["row_count"] == 72, "6h at 5-min resolution is 72 buckets"
    assert res["row_count"] == res["expected_buckets"]
    # The lookback must reach back past `start`, not merely span the window.
    assert query.state.energy.calls[-1]["hours"] >= 16


async def test_lookback_grows_with_window_age(query):
    """A window further in the past needs a proportionally deeper lookback;
    the old code asked for the same 7h no matter how old the window was."""
    seen = []
    for days_ago in (1, 3, 7):
        start = FIXED_NOW - days_ago * 86400
        await query("query_samples", {
            "start_iso": _iso(start), "end_iso": _iso(start + 6 * 3600),
            "bucket_s": 900,
        })
        seen.append(query.state.energy.calls[-1]["hours"])
    assert seen == sorted(seen) and seen[0] < seen[-1]
    assert seen[-1] >= 7 * 24


# ---- 3. the response documents its own resolution ----------------------

async def test_response_reports_resolution_and_counts(query):
    """"No data" and "I asked wrong" have to be distinguishable by the
    model reading this payload."""
    start, end = FIXED_NOW - 6 * 3600, FIXED_NOW
    res = await query("query_samples", {
        "start_iso": _iso(start), "end_iso": _iso(end), "bucket_s": 900,
    })
    for key in ("bucket_s", "requested_bucket_s", "row_count",
                "returned_rows", "truncated", "expected_buckets",
                "window_start", "window_end", "lookback_hours"):
        assert key in res, f"{key} missing from query_samples response"
    assert res["bucket_s"] == 900
    assert res["returned_rows"] == len(res["rows"]) == res["row_count"]


async def test_sub_sampler_bucket_is_clamped_and_reported(query):
    """Asking for finer-than-60s buckets can't conjure resolution that was
    never recorded. Report the resolution actually served so the watts
    aren't silently scaled by a bucket width the DB never used."""
    start, end = FIXED_NOW - 3600, FIXED_NOW
    res = await query("query_samples", {
        "start_iso": _iso(start), "end_iso": _iso(end), "bucket_s": 30,
    })
    assert res["requested_bucket_s"] == 30
    assert res["bucket_s"] == 60
    # Watts must be computed against the served 60s width, not the asked-for
    # 30s one, or every value would read 2x high.
    assert all(r["out_w_avg"] == CONST_OUT_W for r in res["rows"])


async def test_truncation_is_visible(query):
    """row_count reports the true match count even when `rows` is capped,
    so a truncated answer can't be read as a short one."""
    start = FIXED_NOW - 24 * 3600
    res = await query("query_samples", {
        "start_iso": _iso(start), "end_iso": _iso(FIXED_NOW), "bucket_s": 60,
    })
    assert res["row_count"] == 1440
    assert res["truncated"] is True
    assert res["returned_rows"] == advisor_routes._MAX_TOOL_ROWS
    assert len(res["rows"]) == advisor_routes._MAX_TOOL_ROWS


# ---- 4. bad input ------------------------------------------------------

async def test_missing_bounds_error(query):
    res = await query("query_samples", {"bucket_s": 300})
    assert "error" in res


async def test_inverted_window_errors_rather_than_reporting_empty(query):
    """A backwards window previously fell through to an empty row list,
    which reads as 'no telemetry'. It's a caller mistake — say so."""
    res = await query("query_samples", {
        "start_iso": _iso(FIXED_NOW), "end_iso": _iso(FIXED_NOW - 3600),
        "bucket_s": 300,
    })
    assert "error" in res
