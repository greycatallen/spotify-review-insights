"""Step 2 smoke test: a tiny paid run to prove both providers work and to measure Jev token accounting.

Uses 10 reviews from checkpoint_500 rows 101-110: outside cost_100 (whose cold pilot must start with
an empty cache) and disjoint from the golden 50. Separate state file; never exported to grading/.

    python -m pipeline.smoke             # dry run: prints the plan and worst-case cost, no calls
    python -m pipeline.smoke --execute   # paid: ~10 Jev calls + 2 Jev probes + 1 Haiku call
"""
import argparse
import csv
import json
import time

from .config import CHECKPOINT_500_CSV, STATE_DIR, checker
from .db import connect
from .enrich import run_enrichment
from .ingest import ingest
from .ledger import RateLimiter, SpendLedger, now

N, OFFSET = 10, 100
BUDGET = 0.10  # hard cap for this smoke test, USD


def subset_csv(path):
    rows = list(checker.csv_rows(CHECKPOINT_500_CSV))[OFFSET:OFFSET + N]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually make the paid API calls")
    a = ap.parse_args(argv)
    STATE_DIR.mkdir(exist_ok=True)
    src = STATE_DIR / "smoke_input.csv"
    rows = subset_csv(src)
    print(f"Smoke input: {len(rows)} reviews from checkpoint_500 rows {OFFSET + 1}-{OFFSET + N} -> {src}")
    print("Plan: Jev enrich 1 review/request x10 (+2 token-accounting probes); Haiku verify 1 request x10.")
    print(f"Worst-case reservation cap: ${BUDGET:.2f} (expected well under $0.02).")
    if not a.execute:
        print("Dry run only. Re-run with --execute to make the paid calls.")
        return 0

    from .backends.claude import ClaudeBackend
    from .backends.jev import JevBackend, build_questions
    from .verify import run_verify, verification_report

    db = STATE_DIR / "smoke.sqlite"
    for p in (db, db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    con = connect(db)
    ingest(con, src)
    run_id = "smoke-" + now().replace(":", "")
    ledger = SpendLedger(con, BUDGET, run_id)
    jev = JevBackend()
    t0 = time.perf_counter()
    stats = run_enrichment(con, jev, run_id=run_id, phase="initial", ledger=ledger,
                           limiter=RateLimiter(60), batch_size=1, workers=1, max_attempts=2)
    t_enrich = time.perf_counter() - t0
    print(f"\nJev enrich: {stats}")

    # Token-accounting probe: same review, (a) topic question only, (b) full question set.
    probe_text = rows[0]["review_text"]
    full_q, _ = build_questions(probe_text)
    probe = {}
    for name, qs in (("topic_only", {"topic": full_q["topic"]}), ("all_questions", full_q)):
        res = ledger.reserve(jev.estimate_cost([{"text": probe_text}]))
        r = jev.client.system_one(state={"review": probe_text}, questions=qs)
        cost = r.usage.input_tokens * jev.price if r.usage.input_tokens is not None else None
        ledger.settle(res, cost, r.request_id)   # probes count toward the spend ledger too
        probe[name] = {"input_tokens": r.usage.input_tokens, "questions": len(qs), "request_id": r.request_id,
                       "cost_usd": cost}
    print(f"Jev token probe on one review ({len(probe_text)} chars): {probe}")

    claude = ClaudeBackend("claude-haiku-4-5", route="verify")
    items = [{"review_id": r["canonical_id"], "text": r["review_text"]} for r in con.execute(
        "SELECT x.canonical_id, rv.review_text FROM results x JOIN reviews rv ON rv.review_id = x.canonical_id")]
    t1 = time.perf_counter()
    vstats = run_verify(con, claude, items, run_id=run_id, ledger=ledger, primary_config=jev.label_config,
                        batch_size=25)
    t_verify = time.perf_counter() - t1
    rep = verification_report(con, claude.label_config, jev.label_config)

    calls = [dict(r) for r in con.execute("SELECT role, outcome, model, input_tokens, output_tokens, "
                                           "cache_read_tokens, cache_write_tokens, cost_usd, latency_s FROM calls")]
    summary = {
        "run_id": run_id, "reviews": len(rows), "jev_enrich": vars(stats), "jev_enrich_seconds": round(t_enrich, 2),
        "haiku_verify": vstats, "haiku_verify_seconds": round(t_verify, 2), "agreement": rep,
        "jev_token_probe": probe, "calls": calls,
        "ledger_committed_usd": round(ledger.committed(), 6),
        "probe_calls_note": "2 probe calls are not in the calls table (no labels saved); their usage is in jev_token_probe and the spend ledger",
    }
    print("\nLabels (Jev vs Haiku):")
    for r in con.execute("""SELECT x.canonical_id, substr(rv.review_text,1,70) AS t, x.topic, x.intent, x.severity,
                                   x.sentiment, x.needs_review, v.topic AS vt, v.intent AS vi, v.severity AS vs
                            FROM results x JOIN reviews rv ON rv.review_id = x.canonical_id
                            LEFT JOIN verifications v ON v.review_id = x.canonical_id"""):
        print(f"  {r['t']!r:74} jev={r['topic']}/{r['intent']}/{r['severity']} s={r['sentiment']:+.2f} "
              f"nr={r['needs_review']} | haiku={r['vt']}/{r['vi']}/{r['vs']}")
    (STATE_DIR / "smoke_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n{json.dumps({k: summary[k] for k in ('jev_enrich_seconds', 'haiku_verify_seconds', 'ledger_committed_usd')})}")
    print(f"Saved full summary to {STATE_DIR / 'smoke_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
