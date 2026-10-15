# Preserve discharge evidence across fixed SOC bins

Date: 2026-10-15
Status: Proposed

## Context

The empirical correction requires three independent nights of complete,
quality-gated four-hour windows entirely inside one fixed SOC bin. A clean
discharge spanning a bin boundary cannot contribute, leaving higher-SOC
forecasts on the legacy per-pack prior even while comparable discharge data
accumulates. A lower bin can also age out while recent higher-SOC evidence
remains unused. The sunset snapshot now exposes this error instead of hiding
it behind a rolling morning forecast.

A second defect treats the learned charging plateau as an absolute SOC cap.
It can immediately discard already measured energy, including with zero net
power or during discharge. Diversion lookahead must use the same charging
curtailment as the final SOC simulation.

## Decision

Keep existing narrow fits first. Add an empirical pooled fallback using the
same validated continuous four-hour windows between 40% and 95% SOC, with
five independent nights rather than three. Keep the existing capacity,
pack-identity, completeness, freshness, charging, discontinuity, and error
gates unchanged. Keep the upper-quartile and endpoint-uncertainty allowance;
also reject a high-drain night hidden by otherwise stable pooled quartiles.

Apply the pooled coefficient only in the union of observed window ranges.
Do not interpolate across disconnected ranges or extend their endpoints.
Each connected range must retain evidence no more than two days old. Never
use the pool to bypass a specific fit whose projected interval leaves its
validated band. The independent-night count describes the pooled coefficient,
not five complete repetitions at every SOC value inside its support.

Allow an empirical correction during net discharge in twilight, not net solar
charging. A learned ceiling limits positive charging increments without
reducing already measured SOC. Re-simulate future ceiling-limited energy for
diversion safety. Save per-interval baseline values so the presence of a fit
cannot conceal continued use of the legacy pack prior.

No charging target, power limit, mode, default pack loss, or weather coefficient
is retuned. Forecast changes can affect normal controller decisions, so the
existing floor/lookahead guards remain mandatory.

## Validation and limitations

Regression tests cover five-night independence, cross-bin evidence, incompatible
drain regimes, missing SOC ranges, band-exit precedence, twilight, ceiling
energy conservation, and diversion safety. Existing telemetry rejection tests
remain in place. Causal replay uses only pre-decision evidence and preserves
the saved weather/load/efficiency inputs; charged nights are excluded from
accuracy claims. README records both the improvement and residual error.

This is an empirical SOC predictor, not validation of BMS measurements or an
identification of individual physical losses. Unsupported ranges still use
priors; inaccurate readings and insufficient high-SOC evidence remain limits.
Reject lowering a global pack constant to match one night, blindly reusing a
lower-SOC fit at high SOC, and removing data-quality gates to obtain a fit.
