# Cost & runtime calculator (100-review pilot)

**Offline replay is the default.** It needs no API key and makes no calls:

```bash
python3 cost/calculator.py                       # recompute everything from saved measurements
python3 cost/calculator.py --rate-multiplier 2   # what-if: double every API rate
python3 cost/calculator.py --budget 10           # what-if: different budget
```

It writes `usage.csv`, `report.json` and `report.md` from:

| File | What it holds | Edited by |
|---|---|---|
| `pilot_calls.jsonl` | Every real pilot call (cold and warm): role, model, settings, provider usage, timing, outcome | pipeline, never by hand |
| `pilot_runs.json` | Per-run wall clock, stage seconds, counts, input checksum, cache hits | pipeline, never by hand |
| `pilot_records.jsonl` | One result/status per pilot ID, with the row hash and common labels | pipeline |
| `rates.csv` | Dated, editable prices in their billing units, with source links | you |
| `assumptions.json` | Budget, retry/fallback/verify fractions, concurrency, local-compute inputs | you |

Formula: `item_cost = billed_units × price_usd / per_units`, and `total = Σ item_cost` over mutually exclusive items. Anthropic's `input_tokens` already excludes cache reads and writes, which are priced as separate items. A blank price or missing usage is reported as **unknown**, never as zero.

**Explicit paid pilot** (step 3, not run yet): `python -m pipeline.cli pilot --execute --budget <usd>`. The command is added in step 2/3. Without `--execute` it only prints the plan.

Status: **no pilot has been run yet**, so `report.md` says "No measured pilot found".
