# Architecture

```mermaid
flowchart LR
    CSV[(spotify_reviews_18months.csv<br/>660,622 rows)] --> P

    subgraph CODE["Code / SQL (no model)"]
        P[1 Prepare<br/>ingest + row_sha + profile<br/>exact-text dedupe<br/>seeded 100k scope] --> Q[pending queue<br/>(texts without a result)]
        V2[3b Compare<br/>verifier vs primary] 
        G[4 Group<br/>topic.facet membership]
        R[5 Rank<br/>severity_sum, ties by ID]
        X[export grading/ + checker]
        CHK[claim / ID / number audit]
    end

    subgraph MODELS["Model roles (bounded, saved I/O)"]
        E[2 Enrich: Jev jev-1.13.0<br/>1 review / request<br/>topic, intent, severity, sentiment, quote choice]
        F[2b Fallback: Claude Haiku 4.5<br/>≤25 reviews / request, ≤2% of texts]
        VER[3 Verify: Claude Haiku 4.5<br/>1% sample + needs_review, blind to primary]
        N[4b Name issues / request themes<br/>Haiku, bounded examples]
        M[6 Memo + recommendations<br/>Haiku, aggregates + evidence pack only]
    end

    Q --> E -->|validated, saved per batch| S[(state SQLite:<br/>results, calls, spend ledger)]
    E -.invalid twice.-> F --> S
    S --> VER --> V2
    S --> G --> N
    G --> R --> M --> CHK
    S --> X
    R --> X
    CHK --> X

    S --> PUB[publish.py] --> DB[(Neon Postgres<br/>reviews, issues, claims,<br/>aggregates, recommendations)]
    DB --> API[FastAPI on Vercel<br/>read-only /api/*] --> UI[Dashboard<br/>static HTML + Chart.js]
```

| Stage | Owner | Input | Output | Failure behaviour / stop rule |
|---|---|---|---|---|
| 1 Prepare | code + SQLite | CSV path | `reviews`, `texts`, `scope`, `grading/ingestion.json` | strict CSV parser; checksum checked against manifest |
| 2 Enrich | Jev (System One) | one review text | topic, intent, severity, sentiment, quote choice | ≤2 attempts per text (invalid output retried once); spend ledger reserves before dispatch; circuit breaker after 3 × workers consecutive failures; Ctrl+C drains in-flight calls |
| 2b Fallback | Claude Haiku 4.5 | ≤25 texts Jev could not label | same schema, JSON-schema constrained | capped at 2% of distinct texts |
| 3 Verify | Claude Haiku 4.5 | 1% deterministic sample + needs_review (cap 1,200), primary labels hidden | verifier labels; code computes agreement | failures logged; never overwrites primary labels |
| 4 Group | code; Haiku names | complaint/cancellation records | `membership.csv` (one issue per review), issue names | names are cosmetic; membership is deterministic |
| 5 Rank | code | records + membership | `ranking.csv` | `python -m pipeline.cli rank` recomputes from grading/ only |
| 6 Memo / recommend | Claude Haiku 4.5 | ranking, claims, bounded evidence pack | `memo.md`, structured recommendations | code audits claim IDs, issue/review IDs and every number; one corrective retry; leftovers flagged for human review |
| Publish / serve | code; FastAPI | saved state | Postgres tables → `/api/*` → dashboard | backend is read-only and never calls a model |

Where code decides the next step: dedupe and queueing, retries, fallback eligibility and cap, budget
admission, verification sampling, grouping, ranking, export and every count. Where a model judges:
reading review language (enrich, fallback, verify) and writing names and prose from saved aggregates.
