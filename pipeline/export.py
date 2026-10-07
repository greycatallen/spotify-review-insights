"""Export the grading/ adapter folder from a run's SQLite state (all writes atomic).

One record per source ID, in source order. The canonical review of each distinct text carries
the model result; every other review with identical text gets the same labels plus a direct
`cache_source_id` (never a chain). Texts without a valid result are quarantined with a reason.
"""
import json

from .config import AUDIT_VERSION, EMPTY_REASON
from .db import get_meta
from .io_utils import write_csv, write_json, write_jsonl
from .rank import RANKING_HEADER, compute_ranking, ranking_table


def final_results(con, label_configs):
    """text_sha256 -> result row, preferring earlier configs in `label_configs` (primary before fallback)."""
    best = {}
    for cfg in label_configs:
        for r in con.execute("SELECT * FROM results WHERE label_config=?", (cfg,)):
            best.setdefault(r["text_sha256"], dict(r))
    return best


def iter_records(con, label_configs):
    results = final_results(con, label_configs)
    failures = {r["text_sha256"]: r["last_reason"] for r in con.execute(
        "SELECT text_sha256, last_reason FROM failures WHERE label_config=?", (label_configs[0],))}
    for row in con.execute("SELECT review_id, review_text, source_sha256, text_sha256, is_empty FROM reviews "
                           "ORDER BY src_order"):
        base = {"review_id": row["review_id"], "source_sha256": row["source_sha256"]}
        if row["is_empty"]:
            yield {**base, "status": "quarantined", "reason": EMPTY_REASON}
            continue
        res = results.get(row["text_sha256"])
        if res is None:
            why = failures.get(row["text_sha256"], "not yet classified")
            yield {**base, "status": "quarantined", "reason": f"classification_incomplete: {why}"[:300]}
            continue
        rec = {**base, "status": "completed", "topic": res["topic"], "intent": res["intent"],
               "sentiment": res["sentiment"], "severity": res["severity"],
               "entities": json.loads(res["entities"]), "evidence_quote": res["evidence_quote"],
               "needs_review": bool(res["needs_review"]), "label_config": res["label_config"]}
        if res["subtopic"]:
            rec["subtopic"] = res["subtopic"]
        if row["review_id"] != res["canonical_id"]:
            rec["cache_source_id"] = res["canonical_id"]
        yield rec


def completed_ids(con, label_configs):
    return [r["review_id"] for r in iter_records(con, label_configs) if r["status"] == "completed"]


def write_checkpoint(con, label_configs, path):
    """Snapshot of completed IDs (used for checkpoint_before/after around the resume demo)."""
    ids = completed_ids(con, label_configs)
    write_json(path, {"completed_ids": ids, "count": len(ids)})
    return len(ids)


def calls_rows(con):
    for c in con.execute("SELECT * FROM calls ORDER BY started_at, request_id"):
        row = {"request_id": c["request_id"], "role": c["role"], "review_ids": json.loads(c["review_ids"]),
               "model": c["model"], "phase": c["phase"], "outcome": c["outcome"], "label_config": c["label_config"],
               "input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"],
               "provider": c["provider"], "run_id": c["run_id"], "attempt": c["attempt"],
               "settings": json.loads(c["settings"]), "started_at": c["started_at"], "latency_s": c["latency_s"]}
        for k in ("cache_read_tokens", "cache_write_tokens", "billed_units", "billed_unit", "cost_usd", "error"):
            if c[k] is not None:
                row[k] = c[k]
        yield row


def membership_rows(con):
    return [(r["issue_id"], r["review_id"]) for r in
            con.execute("SELECT issue_id, review_id FROM membership ORDER BY issue_id, review_id")]


def export_grading(con, out_dir, label_configs, claims=None, allow_multi_issue=False):
    """Write run.json, records.jsonl.gz, calls.jsonl, membership/ranking/claims. Checkpoints are
    written separately at interruption time and are left untouched here."""
    records = list(iter_records(con, label_configs))
    write_json(out_dir / "run.json", {
        "version": AUDIT_VERSION, "analysis_count": len(records),
        "analysis_sha256": get_meta(con, "input_sha256"),
        "classification_input_fields": ["review_text"], "allow_multi_issue": allow_multi_issue})
    write_jsonl(out_dir / "records.jsonl.gz", records, gz=True)
    (out_dir / "calls.jsonl").unlink(missing_ok=True)       # checker flags a plain + .gz pair as ambiguous
    write_jsonl(out_dir / "calls.jsonl.gz", calls_rows(con), gz=True)

    severity = {r["review_id"]: r["severity"] for r in records if r["status"] == "completed"}
    pairs = membership_rows(con)
    write_csv(out_dir / "membership.csv", ("issue_id", "review_id"), pairs)
    ranking = compute_ranking((iid, rid, severity[rid]) for iid, rid in pairs)
    write_csv(out_dir / "ranking.csv", RANKING_HEADER, ranking_table(ranking))
    write_csv(out_dir / "claims.csv", ("claim_id", "issue_id", "metric", "value"), claims or [])
    status = {"completed": 0, "quarantined": 0, "cache_reuses": 0}
    for r in records:
        status[r["status"]] += 1
        status["cache_reuses"] += "cache_source_id" in r
    return {"records": len(records), **status, "issues": len(ranking), "memberships": len(pairs)}
