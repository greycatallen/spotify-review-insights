"""Measure Jev question sets v1 vs v2 on the same 30 dev reviews: tokens, cost, and label agreement
(with each other and with an independent Haiku labelling). Development-only; separate state file.

Reviews: checkpoint_500 rows 101-130 (outside cost_100, disjoint from the golden 50).

    python -m pipeline.compare_prompts            # dry run, no calls
    python -m pipeline.compare_prompts --execute  # paid: 60 Jev calls + 2 Haiku calls (~$0.02)
"""
import argparse
import csv
import json

from .config import CHECKPOINT_500_CSV, STATE_DIR, checker
from .db import connect
from .enrich import run_enrichment
from .ingest import ingest
from .ledger import RateLimiter, SpendLedger, now

OFFSET, N, BUDGET = 100, 30, 0.10


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args(argv)
    src = STATE_DIR / "compare_input.csv"
    rows = list(checker.csv_rows(CHECKPOINT_500_CSV))[OFFSET:OFFSET + N]
    with open(src, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{N} reviews (checkpoint_500 rows {OFFSET + 1}-{OFFSET + N}); Jev v1 and v2 on each + Haiku on all; cap ${BUDGET}")
    if not a.execute:
        print("Dry run only. Re-run with --execute to make the paid calls.")
        return 0
    from .backends.claude import ClaudeBackend
    from .backends.jev import JevBackend
    from .verify import run_verify

    db = STATE_DIR / "compare.sqlite"
    for p in (db, db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    con = connect(db)
    ingest(con, src)
    run_id = "compare-" + now().replace(":", "")
    ledger = SpendLedger(con, BUDGET, run_id)
    out = {"run_id": run_id, "configs": {}}
    backends = {v: JevBackend(prompt_version=v) for v in ("jevq-v1", "jevq-v2")}
    for v, be in backends.items():
        st = run_enrichment(con, be, run_id=f"{run_id}-{v}", phase="initial", ledger=ledger, limiter=RateLimiter(60),
                            batch_size=1, workers=1, max_attempts=2, log=lambda *_: None)
        tok = con.execute("SELECT avg(input_tokens), sum(input_tokens), sum(cost_usd), avg(latency_s) FROM calls "
                          "WHERE label_config=? AND role='enrich'", (be.label_config,)).fetchone()
        out["configs"][v] = {"stats": vars(st), "avg_input_tokens": round(tok[0], 1), "total_tokens": tok[1],
                             "cost_usd": round(tok[2], 6), "avg_latency_s": round(tok[3], 3)}
    haiku = ClaudeBackend("claude-haiku-4-5", route="verify")
    items = [{"review_id": r["review_id"], "text": r["review_text"]} for r in rows if r["review_text"].strip()]
    items = [it for it in items if con.execute("SELECT 1 FROM texts WHERE canonical_id=?", (it["review_id"],)).fetchone()]
    run_verify(con, haiku, items, run_id=run_id, ledger=ledger, primary_config=backends["jevq-v1"].label_config,
               batch_size=15)

    def labels(cfg):
        return {r["canonical_id"]: (r["topic"], r["intent"], r["severity"]) for r in
                con.execute("SELECT canonical_id, topic, intent, severity FROM results WHERE label_config=?", (cfg,))}
    l1, l2 = labels(backends["jevq-v1"].label_config), labels(backends["jevq-v2"].label_config)
    lh = {r["review_id"]: (r["topic"], r["intent"], r["severity"]) for r in
          con.execute("SELECT review_id, topic, intent, severity FROM verifications")}

    def agree(a, b):
        ids = sorted(set(a) & set(b))
        return {f: round(sum(a[i][k] == b[i][k] for i in ids) / len(ids), 3) for k, f in
                enumerate(("topic", "intent", "severity"))} | {"n": len(ids)}
    out["agreement"] = {"v1_vs_v2": agree(l1, l2), "v1_vs_haiku": agree(l1, lh), "v2_vs_haiku": agree(l2, lh)}
    out["haiku_cost_usd"] = round(con.execute("SELECT sum(cost_usd) FROM calls WHERE role='verify'").fetchone()[0], 6)
    out["ledger_committed_usd"] = round(ledger.committed(), 6)
    text = {r["review_id"]: r["review_text"] for r in rows}
    out["differences"] = [{"text": text[i][:120], "v1": l1[i], "v2": l2.get(i), "haiku": lh.get(i)}
                          for i in sorted(l1) if l1[i] != l2.get(i) or l1[i] != lh.get(i)]
    (STATE_DIR / "compare_summary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps({k: out[k] for k in ("configs", "agreement", "haiku_cost_usd", "ledger_committed_usd")}, indent=2))
    print(f"{len(out['differences'])} reviews with any disagreement; details in {STATE_DIR / 'compare_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
