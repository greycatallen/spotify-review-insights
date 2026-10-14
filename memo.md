# Decision memo: where Spotify's next quarter of product effort should go

**Scope.** Seeded random sample of 100,000 classified Google Play reviews (May 2022 – Nov 2023) out of
660,622 source rows; all source IDs are accounted for in `grading/`. Numbers in brackets are claim
IDs in [`grading/claims.csv`](grading/claims.csv), recomputed by the course checker from saved
records and membership. Counts are reviews, not users.

## Recommendation: prioritize playback reliability

Fix playback interruptions first. Issue `playback.general` ("playback stops and skips unexpectedly")
has **3145 complaint reviews [C10]** with **mean severity 3.386010 [C11]**, the highest mean severity
of any ranked issue, for a priority score of **10649 [C12]**. Severity 3–4 on the shared scale means a
degraded or blocked core task (music stops or will not play), which no other area can compensate for.

Representative reviews: `668bbce6-837e-460e-9168-feb79bf57bc5` ("Keeps stopping doesn't finish end of
songs"); further members are listed on the dashboard and in `grading/membership.csv`.

Suggested actions, in order: (1) investigate and fix playback stops/skips (`playback.general`);
(2) reduce free-tier ad interruption (`usability.ads`); (3) make premium-only boundaries explicit and
revisit gating of basic controls (`billing.premium`, `billing.general`).

## Baseline ranking and alternatives considered

The contract's baseline (priority = complaint count × mean severity) orders the top issues as:

| Rank | Issue | Complaints | Mean severity | Priority |
|---|---|---|---|---|
| 1 | `other.general` — general dissatisfaction | 12556 [C1] | 2.058617 [C2] | 25848 [C3] |
| 2 | `usability.ads` — ad frequency/interruptions | 5084 [C4] | 2.322777 [C5] | 11809 [C6] |
| 3 | `billing.premium` — premium paywall on basics | 4025 [C7] | 2.905590 [C8] | 11695 [C9] |
| 4 | `playback.general` — playback stops/skips | 3145 [C10] | 3.386010 [C11] | 10649 [C12] |
| 5 | `usability.general` — controls/navigation | 3648 [C13] | 2.589090 [C14] | 9445 [C15] |

- **`other.general` (rank 1)** is generic criticism with no specific defect (severity close to 2,
  "dislike"); it is large but not actionable on its own.
- **Ads (`usability.ads`, rank 2)** scores higher than playback on volume, but at mean severity
  2.322777 [C5] most reviews describe annoyance rather than a blocked task, and ad load is a
  business-model trade-off these reviews cannot price. Recommended second, as a free-tier experience
  review. Representative: `0b70547c-6bcd-4b44-bd06-905545ae3123` ("Good App but too many advertisements").
- **Premium paywall (`billing.premium`, rank 3)**, with 2.905590 [C8], reflects restricted basic
  controls behind premium; also recommended (third), together with `billing.general` (1887
  complaints [C16], mean severity 2.722311 [C17]). Representative: `8fa754a4-c29f-4d4b-a1d3-19687a796d01`
  ("Bad very bad after making lyrics viewing only for premium").

Playback is chosen first despite rank 4 because the baseline sums severity, so high-volume
low-severity complaints outrank a smaller set of blocked-core-task complaints; fixing a broken core
task is the prerequisite for the other improvements to matter.

## Limitations

- Self-selected public reviews; not a survey of all users. No revenue, plan tier or confirmed
  cancellations: cancellation language is stated intent, not churn, and no revenue-at-risk is implied.
- Labels are model predictions (Jev). An independent verifier agreed on topic for 0.8338 and on
  intent for 0.8734 of a random 1% sample (`evals/verification/`); agreement is lower on
  low-confidence (`needs_review`) cases.
- Issue grouping is deterministic (topic + matched feature term); issue names are AI-suggested.
- 100,000 of 660,609 nonempty reviews were classified (declared sample); the golden 50 were excluded
  from this run and evaluated separately (`evals/golden/`).

---
*Human review (author).* Drafted by Claude Haiku from saved aggregates
([`evals/memo/memo_ai_draft.md`](evals/memo/memo_ai_draft.md),
[`evals/memo/recommendations_ai.json`](evals/memo/recommendations_ai.json)); both drafts passed the
code audit of claim IDs and numbers. Edits made on review: "users" → "reviews" (the counts are reviews);
the range citation "[C10-C12]" expanded into individual claim IDs; "100000 verified reviews" corrected
to classified; removed the drafts' unsupported churn wording ("causing active churn", "drive immediate
cancellation"); added the baseline table, alternatives and representative review IDs required by the
brief. All numbers above are copied from `grading/claims.csv` or `evals/verification/verification_summary.json`.
