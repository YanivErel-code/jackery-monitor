# November 1, 2026 AC charging reconciliation

Status: unresolved; excluded from SOC-based learning pending independent metering.
All times below are UTC. Settings and physical charge control are unchanged.

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

If reported AC input represents charging power excluding bypassed house load,
subtracting output again would double count that load. Stored energy divided by
gross reported input is approximately 84.5%, whereas dividing by reported net
energy exceeds 100%. These ratios motivate the bypass hypothesis; neither is
a measured charging efficiency. SOC/capacity semantics remain alternative causes.

## Mitigation

Apply a reversible learning exclusion for the affected device from 07:00 to
09:41 (outward minute rounding covers the OFF transition). Preserve all samples,
pack readings, forecasts, control audit and energy totals. Reject adjacent-point
SOC fits touching excluded buckets and invalidate empirical drain annotations.
Do not retune drain, charge efficiency, solar coefficient or sunrise target from
this interval. This mitigation does not prove the cause or retrospectively
correct earlier fitted parameters.

## Remaining measurement

Once a suitable independent meter is available, record synchronized wall-input
energy, device AC/total input and output, all pack SOCs, pack count, and timestamps
over a stable charge interval with a known downstream load. Include a baseline
without that load where practical. Compare wall input against device input and
the load change to establish whether bypass energy is included. Preserve raw
readings and account for conversion losses and SOC quantization. Re-enable
learning only after the energy boundary is established; do not derive efficiency
by assuming the bypass hypothesis is true.
