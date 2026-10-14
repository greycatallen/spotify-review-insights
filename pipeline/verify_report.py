"""Verification report for the final run (offline): agreement between the primary labels (Jev) and the
independent verifier (Claude Haiku, primary labels hidden), split by why each review was sampled.

    python -m pipeline.verify_report
"""
import csv
import json
import random

from .config import ROOT, STATE_DIR
from .io_utils import write_json

PRIMARY = "jev-1.13.0+jevq-v2+a5-common-v1"
VERIFY = "claude-haiku-4-5+claude-v1+a5-common-v1"
SEED, FRACTION = "a5-verify-v1", 0.01


def main():
    import sqlite3
    con = sqlite3.connect(STATE_DIR / "final.sqlite")
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT x.canonical_id, x.topic, x.intent, x.severity, x.needs_review, r.review_text,
                                 v.topic AS vt, v.intent AS vi, v.severity AS vs, v.request_id
                          FROM results x JOIN reviews r ON r.review_id = x.canonical_id
                          LEFT JOIN verifications v ON v.review_id = x.canonical_id AND v.label_config = ?
                          WHERE x.label_config = ?""", (VERIFY, PRIMARY)).fetchall()
    # Re-derive the sampling reason with the same seeded rule as verify.sample_for_verification.
    rng = random.Random(SEED)
    reason = {r["canonical_id"]: ("random_1pct" if rng.random() < FRACTION else
                                  "needs_review" if r["needs_review"] else None) for r in rows}
    checked = [r for r in rows if r["vt"] is not None]
    groups = {"all_verified": checked,
              "random_1pct": [r for r in checked if reason[r["canonical_id"]] == "random_1pct"],
              "needs_review_only": [r for r in checked if reason[r["canonical_id"]] == "needs_review"]}
    pct = lambda rs, a, b: round(sum(r[a] == r[b] for r in rs) / len(rs), 4) if rs else None
    summary = {g: {"n": len(rs), "topic": pct(rs, "topic", "vt"), "intent": pct(rs, "intent", "vi"),
                   "severity_exact": pct(rs, "severity", "vs"),
                   "severity_within_1": round(sum(abs(r["severity"] - r["vs"]) <= 1 for r in rs) / len(rs), 4) if rs else None}
               for g, rs in groups.items()}
    summary["sampling"] = ("Seeded 1% random sample of distinct texts plus every needs_review text (low Jev confidence), "
                           "capped at 1,200 in review-ID order; verifier saw only the review text and rubric.")
    out = ROOT / "evals" / "verification"
    write_json(out / "verification_summary.json", summary)
    with open(out / "disagreements.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["review_id", "sampled_because", "text", "primary_topic", "verifier_topic", "primary_intent",
                    "verifier_intent", "primary_severity", "verifier_severity", "verifier_request_id"])
        for r in checked:
            if (r["topic"], r["intent"]) != (r["vt"], r["vi"]):
                w.writerow([r["canonical_id"], reason[r["canonical_id"]], r["review_text"][:300], r["topic"], r["vt"],
                            r["intent"], r["vi"], r["severity"], r["vs"], r["request_id"]])
    print(json.dumps({k: v for k, v in summary.items() if k != "sampling"}, indent=1))


if __name__ == "__main__":
    main()
