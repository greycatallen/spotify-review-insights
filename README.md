# Spotify Insight Pipeline — Assignment 5

Turns Spotify Google Play reviews (May 2022 – Nov 2023) into a product recommendation backed by
traceable evidence: code prepares and counts, Jev labels each review, Claude Haiku independently
verifies a sample and writes from saved aggregates, and every number is checked in code.

**Live dashboard:** https://spotify-review-insights-nine.vercel.app — no login needed. The backend
(`/api/*`, FastAPI on Vercel) reads only from a Neon Postgres database loaded from the saved run;
it never calls a model. API docs: https://spotify-review-insights-nine.vercel.app/api/docs

**Interrupt/resume recording:** see the
[release assets](../../releases/tag/v1.0-submission) (`1_initial_run_and_interrupt.mov`,
`2_checkpoint_and_resume.mov`).

> **Declared scope.** The updated brief allows "all 660,622 (or at least 100,000)" reviews. This
> submission **ingests, profiles and accounts for all 660,622 source rows**, and **classifies a seeded
> random sample of 100,000 nonempty reviews** (`SHA-256("a5-final-100k-v1:" + review_id)`, lowest
> 100,000, golden 50 excluded; ID-list hash `bbaefebb…ccb`). Every other row is exported as
> `quarantined` with reason `not_in_scope` (13 empty texts as `empty_review_text`). The coverage
> formula still divides by 660,609, so the checker reports the out-of-scope rows as
> `unfinished_classification` by design. Deviation from the brief: the golden 50 texts are **not**
> in the final run (they were run separately for evaluation, `evals/golden/`).

## Results (final run, 2026-10-14)

| | |
|---|---|
| Source rows ingested and profiled | **660,622** (`grading/ingestion.json`, provided helper; matches manifest) |
| Records exported | **660,622**: 100,000 completed, 13 `empty_review_text`, 560,609 `not_in_scope` (0 missing / duplicate / invalid) |
| Classified in scope | **100,000 / 100,000** (77,944 distinct texts; **22,056** exact-text reuses with direct `cache_source_id`) |
| Enrichment calls | 77,944 succeeded + 79 failed (timeouts, all retried successfully); 0 invalid outputs; fallback not needed |
| Golden 50 (separate run) | topic **88%**, intent **92%**, severity exact **86%** (MAE 0.14), sentiment within 0.5 **90%**, quotes 50/50 exact — [report](evals/golden/report.md) |
| Independent verification | 1,199 reviews re-labelled blind by Haiku. Random 1%: topic **83%**, intent **87%**, severity exact 81%; low-confidence (`needs_review`) cases: 57% / 62% — [summary](evals/verification/verification_summary.json), [disagreements](evals/verification/disagreements.csv) |
| Planted errors / injection | 20/20 planted wrong labels flagged by the verifier comparison; 6/6 malformed records rejected; injected instruction ignored — [planted](evals/planted/planted_results.json) |
| Cost (measured) | Final run **$3.3309** (Jev enrich $2.7046 + Claude verify/group/memo/recommend $0.6263); whole project incl. pilots, dev runs and golden eval ≈ $3.5 |
| Time (measured) | Enrichment 33.9 min wall clock (6 workers, ~38–60 reviews/s); finish stage ~25 min (verification dominated) |
| Interrupt/resume | initial phase stopped by Ctrl+C after 586 texts (`checkpoint_before.json`: 10,745 review IDs incl. reuses); resume completed the rest with **no** resent IDs (`checkpoint_after.json`: 100,000) |
| Course checker | `status: review_required` with exactly two expected flags: `unfinished_classification` (out-of-scope rows, by design) and `invalid_usage` ×152 (timed-out calls whose token usage is genuinely unknown, kept as `null`). Coverage, ranking, claims, cache reuse, roles and resume all pass. The checker's golden score is 0/50 because the golden texts are excluded from the final run by design — see the separate golden evaluation above. |

**Decision:** prioritize playback reliability, then free-tier ad load, then premium paywall
boundaries — see [memo.md](memo.md) (human-reviewed; AI drafts kept in `evals/memo/`).

### One review traced end to end

1. **Source:** `019a9dfc-8d1f-4f30-90c6-a9920a5599e6` — "It wouldn't even let me play the song I wanted"
   (row hash `5232b777…f0cd`), in the seeded 100k scope.
2. **Enrich:** Jev request `req_01a13b9894ec797080fad16122ee96e0` (phase `resume`) → playback /
   complaint / severity 4; quote = full text (exact substring), `label_config`
   `jev-1.13.0+jevq-v2+a5-common-v1`.
3. **Verify:** Haiku request `req_011Cg2bXSNNiyKTvjVaFLPnf`, without seeing Jev's answer →
   playback / complaint / 4 (agrees).
4. **Group:** code assigns it to `playback.general` (`grading/membership.csv`).
5. **Rank:** `playback.general` is rank 4, 3145 complaints, mean severity 3.386010, priority 10649
   (`grading/ranking.csv`).
6. **Memo:** cited as claims C10–C12 in [memo.md](memo.md) and on the dashboard.

**An ambiguous case:** golden review `5de7f95b…` ("We are boycotting Swedish app in our protest…"):
human label other/complaint/2, Jev other/cancellation/2. The contract says bare boycott slogans are
`unclear` unless a personal departure is stated; "we are boycotting" was read as a departure by Jev
and as a complaint by the labeller. Recorded as a disagreement, not re-labelled
([golden report](evals/golden/report.md)).

## Rubric map

| Rubric item | Evidence |
|---|---|
| **D1** accessible code/setup and artifacts | [Setup](#setup), `requirements*.txt`, `.env.example`, `grading/`, `cost/`, live dashboard |
| **D2** architecture, shared schema, provenance | [docs/architecture.md](docs/architecture.md), `pipeline/rubric.py` (shared definitions), `pipeline/schema.py`, `label_config` + `source_sha256` + `cache_source_id` on every record |
| **D3** memo numbers linked to calculations | [memo.md](memo.md) cites `grading/claims.csv` IDs; code audit in `run_summary.json` → `stages.memo.audit`; `python -m pipeline.cli rank` recomputes the ranking |
| **D4** recommendation, alternatives, limitations | [memo.md](memo.md); dashboard "AI recommendations" (alternatives + limits, every ID/number code-checked) |
| **T1** 50 human labels, per-field comparison, error analysis | [evals/golden/report.md](evals/golden/report.md), `golden/golden_50_labeled.csv`, `golden/label_fixes.csv` |
| **T2** independent verification, planted errors, injection | `run_summary.json` → `verification`; [evals/planted/planted_results.json](evals/planted/planted_results.json); synthetic injection in [evals/golden/report.md](evals/golden/report.md); [evals/test_suite_report.txt](evals/test_suite_report.txt) |
| **T3** real 100-review cold/warm pilot, calculator, controls | [cost/README.md](cost/README.md), [cost/report.md](cost/report.md), `cost/pilot_*`, `cost/pilot_attempt1/`, `cost/dev/` |
| **W1** ingestion, coverage, classification | `grading/ingestion.json` (provided helper), `grading/records.jsonl.gz`, `self-check` below |
| **W2** staged program, bounded calls, saved handoffs, resume | `pipeline/final.py`, `grading/calls.jsonl.gz`, `grading/checkpoint_before.json` / `checkpoint_after.json`, recording |
| **W3** reproducible ranking + deployed dashboard | `grading/ranking.csv`, `python -m pipeline.cli rank`, live dashboard + `api/` + `pipeline/publish.py` |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-pipeline.txt
cp .env.example .env        # fill TYPESAFE_API_KEY and ANTHROPIC_API_KEY only for paid commands
```

Place the course dataset folder at `Final Assignment - Spotify Reviews Dataset/` (the 97.4 MB CSV is
not committed; SHA-256 `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6`, source:
the course ZIP / [Kaggle, BwandoWando v2](https://www.kaggle.com/datasets/bwandowando/3-4-million-spotify-google-store-reviews), CC0).

### No-API commands (graders)

```bash
python3 cost/calculator.py                         # offline cost/runtime replay (no key, no calls)
python3 cost/calculator.py --rate-multiplier 2     # doubling rates doubles API spend only
.venv/bin/python -m pipeline.cli rank              # recompute ranking from grading/ files only
.venv/bin/python -m pipeline.cli check             # zero-API course checker -> self-check.json
.venv/bin/python -m pipeline.evaluate --report-only  # recompute golden metrics from saved predictions
.venv/bin/python -m pytest tests                   # 36 offline tests (fake backend, no calls)
```

### Paid commands (explicit `--execute`; dry run otherwise)

```bash
.venv/bin/python -m pipeline.pilot --execute                       # 100-review cold/warm pilot
.venv/bin/python -m pipeline.devrun --set 500 --workers 1 --execute
.venv/bin/python -m pipeline.final prepare                         # free: ingest + seeded 100k scope
.venv/bin/python -m pipeline.final enrich --phase initial --execute
.venv/bin/python -m pipeline.final checkpoint before
.venv/bin/python -m pipeline.final enrich --phase resume --execute
.venv/bin/python -m pipeline.final checkpoint after
.venv/bin/python -m pipeline.final finish --execute                # verify, group, memo, export
.venv/bin/python -m pipeline.publish --state state/final.sqlite --db "$DATABASE_URL"
```

Any input CSV works with the same stages (`pipeline.cli ingest --input <csv> --state <file>`,
`pipeline.devrun`, `pipeline.pilot`).

## Design choices (measured)

- **Prepare/dedupe in SQLite** before any model call: 660,622 rows → 484,189 distinct nonempty texts
  (100k scope → 77,944). Exact duplicates reuse one validated result via `cache_source_id`.
- **Jev for fixed labels.** Smoke test measured ~1,000 input tokens/review; trimmed question set
  `jevq-v2` measured 810 (−18%) with 100% topic/intent agreement vs v1 on 30 dev reviews
  (`state` comparison summarised in `cost/pilot_attempt1/NOTES.md` and commit history). Jev bills
  input tokens only ($0.042/Mtok); Haiku measured ~13× more per review, so Haiku is reserved for
  verification, capped fallback (≤2%), naming and writing.
- **One review per Jev request** (Jev judges one state per request) — within the ≤50 limit.
  Claude requests carry ≤25 reviews.
- **Workers:** 1 → 8.7 reviews/s, 2 → 17.9 reviews/s at identical cost on the same 500 reviews
  (`cost/dev/`); final run used 6 workers with a shared rate limiter and spend ledger.
- **Controls:** worst-case reservation before every call; per-provider caps (Jev $3.80, Claude
  $2.00); invalid output retried once; circuit breaker; Ctrl+C drains and saves in-flight calls;
  timed-out calls keep their reservation as `unreconciled` spend.
- **What failed along the way** (kept as evidence): pilot attempt 1 was throttled by our own rate
  limiter and its memo rounded claim values (fixed: `memo-v2`, audit + one corrective retry); the
  first final initial phase ran at ~3 reviews/s because the pending-queue query ran once per
  request on the 660k-row DB (fixed: 2,000-row prefetch, 0.87 s → 0.28 s per page); a provider
  stall tripped the circuit breaker once (threshold scaled to 3 × workers); timed-out calls have
  unknown usage, so `input_tokens` stays `null` and the checker reports it as missing usage
  instead of an invented 0.

## Limitations

- Self-selected public reviews; no revenue, plan tier or confirmed cancellations. Cancellation
  language is stated intent, not churn. First (2022-05) and last (2023-11) months are partial.
- Labels are model predictions; Jev is not fully deterministic (two cold runs of the same 479
  texts agreed 98.5% on topic). Saved results are reused, so the submitted labels are reproducible.
- Grouping is deterministic (topic + first matched feature term), so broad buckets like
  `other.general` remain large; issue names are AI-suggested.
- 100,000 of 660,609 nonempty reviews are classified (declared scope above).
