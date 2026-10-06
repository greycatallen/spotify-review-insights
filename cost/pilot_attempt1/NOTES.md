# Pilot attempt 1 (2026-10-06) — kept as evidence, superseded

Real paid cold/warm run on cost_100.csv. Cost $0.023807 (Jev enrich $0.003445, Haiku verify $0.009555,
group $0.006497, memo $0.004310). 100/100 completed, 0 failed; warm run made 0 enrichment calls.

Problems found and fixed before attempt 2:
1. Enrich wall time (40 s) was limited by our own rate limiter (60 requests/min), not by Jev latency.
   Raised to 3,000/min (Jev documents 80 requests/s).
2. Memo (memo-v1) rounded claim values (1.909091 -> 1.91) and converted agreement fractions to
   percentages; the code audit flagged them as unexplained numbers. memo-v2 requires exact copies and
   allows one corrective retry.
3. cost/rates.csv priced Jev per classification; Jev bills input tokens ($0.042/Mtok). Fixed in rates.csv.
