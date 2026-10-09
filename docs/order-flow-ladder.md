# Order-flow threshold ladder (of-v2a)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Ticket:
> [#188](https://github.com/ibrahimjaved12/crypto-watcher/issues/188).

## What the question tests

The of-v1 rule `of_cum240_4h` enters in the direction of a strong taker imbalance. The
imbalance is taken over the last 240 minutes and converted to a robust z against 30 days of
hourly values. A trade fires when |z| crosses 2 from below; the rule then stays disarmed until
|z| < 1, with a 240-minute cooldown. The of-v1 question used a single target (rr 1.5).

of-v2a asks two things:

1. Does a stricter crossing threshold select better trades?
2. Do larger targets change the result?

It is a separate experiment family, `order-flow-v2` (strategy version `of-v2`,
`python/market_analysis/benchmark/order_flow_v2.py`). The of-v1 family, its question and its
ledger hashes are unchanged. The signal code is the of-v1 `cum240` function, reused as is.

## The 12 variants

All variants use the 240-minute horizon and k = 2 on the existing lb1 labels.

| Strategy | Crossing threshold c | Targets (rr) |
| --- | --- | --- |
| `of2_cum240_c20` | 2 (replicates `of_cum240_4h`) | 1.5, 2, 3 |
| `of2_cum240_c25` | 2.5 | 1.5, 2, 3 |
| `of2_cum240_c30` | 3 | 1.5, 2, 3 |
| `of2_cum240_c35` | 3.5 | 1.5, 2, 3 |

The question has K = 4 strategies x 3 targets = 12 variants, and the pass bar uses K = 12. The
c = 2 rows replicate of-v1. Their rr 1.5 cell must reproduce the of-v1 signals exactly; a
unit test checks this.

The ladder also implies a dose-response hypothesis: net R rises with c. That needs the
screening module from #220 and is not tested by this question.

Disarm and cooldown state depend on the threshold. A stricter threshold therefore does not
simply select a subset of the c = 2 signals, and the signal sets are not nested.

## Time exits at rr 3

With k = 2 and the 4 x horizon time limit, SPEC-1 section 4 gives the driftless-Brownian
share of trades that end at the time limit, assuming correctly scaled sigma:

| rr | Expected time-limit share |
| --- | --- |
| 1.5 | about 55% |
| 2 | about 64% |
| 3 | about 68% |

The rr 3 cells are mostly time exits by construction. Their result depends heavily on the
drift over the 16-hour window and much less on whether the target is reached. Read those
cells as a test of drift, not of target placement.

## Underpowered variants

Higher thresholds produce fewer signals, and the c = 3.5 rows may have few trades. The steps
are:

1. **Count first.** Register the question from the private drafts, then run `count`
   (outcome-blind signal counts) before `run`.
2. **Treat low power as no evidence.** A variant whose minimum detectable edge (SPEC-1
   section 2) is above a plausible edge is reported as underpowered. It shows as
   NOT_ENOUGH_EVIDENCE, not as a failure. A strict threshold that cannot be judged says
   nothing about whether it works.

No results are recorded here. Reports and the experiment ledger live in the private
research-data repo.
