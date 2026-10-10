# Positioning family (positioning-v1): fade the account long/short ratio

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#188](https://github.com/ibrahimjaved12/crypto-watcher/issues/188).

## Why this question exists

The positioning screen ([screening](./screening.md)) found one series with a signal on development:
`global-ls` (`count_long_short_ratio`, the all-account long/short ratio of the Binance metrics
archive) had a negative slope on forward returns with day-clustered errors (60 m t about -3.6,
240 m t about -3.1). The other three positioning series showed nothing. That is an exploratory
finding on the same data, so this question is pre-registered with the **direction fixed as fade**
and is judged on development first, then validation. The hidden stretch stays untouched.

## The rule (`python/market_analysis/benchmark/positioning.py`)

- **Value:** `count_long_short_ratio` from `metrics_lake.load_symbol_metrics_range` (hidden months
  refused before any file is opened). A row stamped `create_time` is usable from `create_time + 5
  min`; an hourly decision at t uses the row usable exactly at t. A missing row or empty value drops
  the decision; it is never filled from an older row.
- **z:** robust z (`order_flow.robust_z`, 1.4826 MAD) against the values at the previous hourly
  decisions of the trailing 30 days (at least 500), exactly the screen's `global-ls` z.
- **Signal:** while armed, z >= theta gives SHORT, z <= -theta gives LONG; a signal disarms until
  |z| < 1; cooldown 240 minutes (a suppressed trigger changes no state).
- **Geometry:** 240-minute horizon, k = 2, rr indices 1, 2, 3 (rr 1.5, 2, 3).

| Strategy | theta | Targets (rr) |
| --- | --- | --- |
| `pos_gls_fade_c20` | 2 | 1.5, 2, 3 |
| `pos_gls_fade_c25` | 2.5 | 1.5, 2, 3 |
| `pos_gls_fade_c30` | 3 | 1.5, 2, 3 |

K = 3 strategies x 3 targets = 9 variants. Questions of this family are `question-v2` with
`sigma_model = ewma-robust-hcal` (lb3h labels, P16); the family refuses any other label model.

## Running it

The hypothesis text is read from the private draft `drafts/<question-id>.hypothesis.txt` of the
research-data repo, never from workflow inputs. `Experiment run` with family `positioning-v1`
(sigma model `family-default`): `register`, then `count` on development, then `run` on development
and validation. The run downloads the mx metrics releases (revision 1) of 2024-01 through the
segment's last month next to the rd bars.
