"""Step 3: the measured 100-review pilot (COST_CALCULATOR.md section 2), cold then warm.

Cold: empty state file -> ingest cost_100.csv (checksum verified) -> enrich (Jev, 1 worker) ->
capped fallback -> declared verification sample (Haiku) -> group (code + Haiku naming) -> rank ->
memo (Haiku). Warm: identical settings on the saved state; must make zero enrichment calls, and
downstream stages reuse their input-hash cache.

Writes cost/pilot_calls.jsonl, cost/pilot_runs.json, cost/pilot_records.jsonl, cost/pilot_memo.md,
then runs the offline calculator.

    python -m pipeline.pilot                      # dry run: plan + worst-case reservations, no calls
    python -m pipeline.pilot --execute            # paid: cold then warm
"""
import argparse
import json
import time

from .config import COST_100_CSV, COST_DIR, MANIFEST, STATE_DIR, checker
from .db import connect
from .enrich import run_enrichment
from .export import iter_records
from .group import assign_membership, name_issues
from .ingest import ingest
from .io_utils import write_json, write_jsonl
from .ledger import RateLimiter, SpendLedger, now
from .memo import write_memo
from .verify import run_verify, sample_for_verification, verification_report

PILOT_BUDGET = 0.50          # hard cap for cold + warm, USD
VERIFY_FRACTION = 0.20       # declared verification sample for the pilot (plus every needs_review record)
FALLBACK_CAP_FRACTION = 0.02 # at most 2% of distinct texts may go to the fallback route
WORKERS, JEV_BATCH, CLAUDE_BATCH = 1, 1, 25


def run_once(con, kind, ledger_cap, log=print):
    from .backends.claude import ClaudeBackend
    from .backends.jev import JevBackend
    run_id = f"pilot-{kind}-" + now().replace(":", "").replace("-", "")
    ledger = SpendLedger(con, ledger_cap, run_id)
    jev = JevBackend()
    fallback = ClaudeBackend("claude-haiku-4-5", route="fallback")
    verifier = ClaudeBackend("claude-haiku-4-5", route="verify")
    configs = [jev.label_config, fallback.label_config]
    stage, t_all = {}, time.perf_counter()

    t = time.perf_counter()
    s1 = run_enrichment(con, jev, run_id=run_id, phase="initial", ledger=ledger, limiter=RateLimiter(3000, 5_000_000),
                        batch_size=JEV_BATCH, workers=WORKERS, max_attempts=3, done_configs=configs, log=log)
    n_texts = con.execute("SELECT count(*) FROM texts").fetchone()[0]
    s2 = run_enrichment(con, fallback, run_id=run_id, phase="initial", ledger=ledger, limiter=RateLimiter(50),
                        batch_size=CLAUDE_BATCH, workers=1, max_attempts=3, done_configs=configs,
                        fallback_from=jev.label_config, fallback_cap=max(1, int(n_texts * FALLBACK_CAP_FRACTION)),
                        log=log)
    stage["enrich"] = round(time.perf_counter() - t, 3)

    t = time.perf_counter()
    sample = sample_for_verification(con, jev.label_config, VERIFY_FRACTION)
    v = run_verify(con, verifier, sample, run_id=run_id, ledger=ledger, primary_config=jev.label_config,
                   batch_size=CLAUDE_BATCH, log=log)
    stage["verify"] = round(time.perf_counter() - t, 3)

    t = time.perf_counter()
    counts = assign_membership(con, configs)
    g = name_issues(con, configs, run_id=run_id, ledger=ledger)
    stage["group"] = round(time.perf_counter() - t, 3)

    records = list(iter_records(con, configs))
    sev = {r["review_id"]: r["severity"] for r in records if r["status"] == "completed"}
    vr = verification_report(con, verifier.label_config, jev.label_config)
    context = {"reviews_in_pilot": len(records), "completed": len(sev),
               "quarantined": sum(r["status"] == "quarantined" for r in records),
               "verification_sample": vr["compared"], "verification_topic_agreement": vr["topic_agreement"],
               "verification_intent_agreement": vr["intent_agreement"]}
    t = time.perf_counter()
    m = write_memo(con, sev, context, run_id=run_id, ledger=ledger)
    stage["memo"] = round(time.perf_counter() - t, 3)
    wall = round(time.perf_counter() - t_all, 3)

    calls = [dict(r) for r in con.execute("SELECT * FROM calls WHERE run_id=? ORDER BY started_at", (run_id,))]
    run = {"run_id": run_id, "run_kind": kind, "started_at": now(), "input_sha256": checker.sha(COST_100_CSV),
           "input_rows": len(records), "completed": len(sev), "failed": sum(
               r["status"] == "quarantined" and r.get("reason") != "empty_review_text" for r in records),
           "quarantined": sum(r["status"] == "quarantined" for r in records), "unique_texts": n_texts,
           "result_cache_hits": len(sev) - sum(len(json.loads(c["review_ids"])) for c in calls
                                               if c["role"] == "enrich" and c["outcome"] == "succeeded"),
           "workers": WORKERS, "batch_size": {"jev": JEV_BATCH, "claude": CLAUDE_BATCH}, "wall_seconds": wall,
           "stage_seconds": stage, "label_config": jev.label_config,
           "settings": {"jev": jev.settings, "fallback": fallback.settings, "verify": verifier.settings,
                        "verify_fraction": VERIFY_FRACTION, "fallback_cap_fraction": FALLBACK_CAP_FRACTION},
           "enrich_stats": {"primary": vars(s1), "fallback": vars(s2)}, "verify_stats": v, "group": g,
           "issues": len(counts), "memo_audit": m["audit"], "memo_cached": m["cached"], "memo_attempts": m["attempts"],
           "ledger_committed_usd_after": round(ledger.committed(), 6)}
    return run, calls, records, m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="make the paid calls (cold then warm)")
    a = ap.parse_args(argv)
    want = json.loads(MANIFEST.read_text())["files"]["cost_100.csv"]["sha256"]
    got = checker.sha(COST_100_CSV)
    print(f"cost_100.csv sha256 {got[:16]}… matches manifest: {got == want}")
    print(f"Plan: cold (empty cache) then warm; Jev jevq-v2 1 review/request, {WORKERS} worker; fallback cap "
          f"{FALLBACK_CAP_FRACTION:.0%}; verify {VERIFY_FRACTION:.0%} sample + needs_review via Haiku; "
          f"group naming + memo via Haiku. Hard cap ${PILOT_BUDGET:.2f}. Expected ~$0.02–0.05.")
    if got != want:
        raise SystemExit("cost_100.csv checksum mismatch; refusing to run")
    if not a.execute:
        print("Dry run only. Re-run with --execute to make the paid calls.")
        return 0
    db = STATE_DIR / "pilot_100.sqlite"
    for p in (db, db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)   # cold run starts from an empty result cache
    con = connect(db)
    t = time.perf_counter()
    ing = ingest(con, COST_100_CSV)
    ingest_s = round(time.perf_counter() - t, 3)
    want_ids = [r["review_id"] for r in checker.csv_rows(COST_100_CSV)]

    runs, all_calls = [], []
    for kind in ("cold", "warm"):
        print(f"\n=== {kind} run ===")
        run, calls, records, memo = run_once(con, kind, PILOT_BUDGET)
        run["ingest_seconds"] = ingest_s if kind == "cold" else 0.0
        run["ids_match_cost_100"] = [r["review_id"] for r in records] == want_ids
        runs.append(run)
        for c in calls:
            row = {**c, "run_kind": kind, "review_ids": json.loads(c["review_ids"]),
                   "settings": json.loads(c["settings"]), "extra": json.loads(c["extra"] or "{}"), "tier": "standard"}
            row["route"] = "fallback" if c["role"] == "enrich" and c["provider"] != "typesafe" else "primary"
            all_calls.append(row)
        print(json.dumps({k: run[k] for k in ("wall_seconds", "stage_seconds", "completed", "failed",
                                              "result_cache_hits", "memo_audit", "ledger_committed_usd_after")}, indent=1))
        if kind == "cold":
            write_jsonl(COST_DIR / "pilot_records.jsonl", records)
            (COST_DIR / "pilot_memo.md").write_text(memo["memo"] + "\n", encoding="utf-8")
            write_json(COST_DIR / "pilot_claims.json", {"claims": memo["claims"], "audit": memo["audit"]})
    write_jsonl(COST_DIR / "pilot_calls.jsonl", all_calls)
    write_json(COST_DIR / "pilot_runs.json", runs)
    warm_enrich = sum(1 for c in all_calls if c["run_kind"] == "warm" and c["role"] == "enrich")
    print(f"\nWarm-run enrichment calls: {warm_enrich} (must be 0). Ingest: {ing}")
    print("Now run the offline calculator:  python3 cost/calculator.py")
    return 0 if warm_enrich == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
