# November 1, 2026 AC charging reconciliation

Status: accounting boundary confirmed by the user; model correction supersedes
the temporary learning exclusion. All interval times below are UTC.

## Evidence

The saved 07:00 smart-charge decision has system SOC 35.3333%; the 09:40:20
decision has 42.3333%, with a stable 30,240 Wh configured system capacity and
five expansion packs. The implied stored-energy increase is 2,116.8 Wh.
The 160 raw minute buckets from 07:00 inclusive to 09:40 exclusive sum to
2,505.79 Wh input and 1,224.19 Wh output: 1,281.60 Wh net. Solar is negligible.
The approximately 835 Wh residual cannot be reconciled by positive charging
losses under these accounting assumptions. This independently reproduces the
advisor's five-minute aggregation to rounding/bucket-boundary differences.

Charging control executed ON at 07:00 and OFF at 09:40:20. All expansion-pack
SOCs rise across those saved decisions; both HTTP and MQTT pack observations
exist within the interval. A simple source-switch jump does not explain it.

The AC charging plug at 192.168.3.109 reports model EP10, alias House,
and `power_w=None`. The user confirmed no independent meter is available.
Device-reported AC power is therefore not independently verified.

The user subsequently confirmed that reported input feeds the battery, while
house output is supplied through AC bypass. Total wall input is input plus
output. The AC port supports 1,800 W total; the user requested a 1,750 W planning
limit. Subtracting bypassed output was the accounting error. With this boundary,
2,116.8 / 2,505.79 = approximately 84.5% SOC-implied charging efficiency for the
full interval, and wall energy is about 3.730 kWh. SOC quantization and residual
losses still limit precision; this does not establish independently metered
efficiency or justify pinning the model to one interval's ratio.

## Initial mitigation

Apply a reversible learning exclusion for the affected device from 07:00 to
09:41 (outward minute rounding covers the OFF transition). Preserve all samples,
pack readings, forecasts, control audit and energy totals. Reject adjacent-point
SOC fits touching excluded buckets and invalidate empirical drain annotations.
Do not retune drain, charge efficiency, solar coefficient or sunrise target from
this interval. This mitigation does not prove the cause or retrospectively
correct earlier fitted parameters.

## Correction

Configure battery-side AC metering and the 1,750 W shared supply limit for the
affected device. Fit complete AC-charging hours with input as the denominator;
skip mixed ON/OFF hours, and retain solar-only net-energy accounting. Preserve
raw energy, SOC, historic decisions and forecast records. Once the corrected
code and configuration are live, restore the 07:00–09:41 learning interval.

The conditional charging forecast must use learned battery-charge power capped
by 1,750 W minus predicted house output. While AC bypass is active, house output
does not drain the battery. Hours whose house output exceeds the supply limit
cannot be selected. Solar remains separate from AC wall power. Rank charging
slots using total wall energy, including bypassed household output.

An offline replay of the current 14-day history produced a fitted efficiency of
85.2% from 100 qualifying windows with the interval restored, versus 89.4% from
115 windows under the old denominator and exclusion. The fit is data-derived;
no manual efficiency, drain or sunrise-target adjustment is required. Independent
metering remains useful validation, rather than a prerequisite to correcting
the user-confirmed accounting boundary. The planning limit does not program the
Jackery's hardware input-current setting or replace its overload protection.
