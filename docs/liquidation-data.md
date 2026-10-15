# Liquidation data probe

CLAUDE.md defers liquidations: there is no free contiguous source known, and a paid purchase
needs the owner's explicit OK. The Binance public archive (`data.binance.vision`) publishes a
`liquidationSnapshot` family for USD-M futures, but what it holds is unverified. The probe
workflow (`.github/workflows/liquidation-probe.yml`, `scripts/research/liquidation_probe.py`)
answers the questions below and nothing else. It publishes and stores nothing; the public job
summary carries counts, dates, byte totals, the observed header and counts of distinct values of
text columns, never row values. The data is public market data.

## Questions the probe answers

1. **Coverage.** Per symbol and family (`daily`, `monthly`) under
   `data/futures/um/<family>/liquidationSnapshot/<SYMBOL>/`: how many zip keys exist, which are
   the first and last periods, which periods are missing between them, and how many compressed
   bytes the family holds (listed with the S3 API and followed by marker until exhausted).
2. **Layout.** Does every zip have a `.CHECKSUM`, and are there other keys in the folder?
3. **Header and shape.** The observed header line, row count and first/last timestamp of the
   first, middle and last zip of each symbol (daily if available, else monthly). Downloads are
   limited to periods up to 2025-12 so the hidden stretch is never read; the listing itself covers
   every key. The timestamp column is the first header column containing "time" with an integer
   first value; its unit is guessed from magnitude (seconds, milliseconds or microseconds).
4. **Categorical content.** Counts of distinct values of every text column with at most 12
   distinct values (side, order status, order type, time in force, ...). Columns whose first value is
   numeric are not tabulated.
5. **Duplication.** Share of rows identical to the previous row. A share near 0.5 means each event
   is stored twice in a row (a pairwise duplicate).

## What the probe does not answer

Whether the snapshots are the full liquidation stream or a sample, the price/quantity
semantics, or any predictive value. Those need a follow-up once the layout is known.

## Conventions

Downloads and checksum checks mirror the metrics archive: a 404 is a recorded gap, a checksum
mismatch is an error. Output is a table of findings; any release or storage of the data is a
separate decision for the owner.
