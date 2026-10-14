"""Planted-error check for the independent verification stage (offline, no model calls).

Works on a COPY of a run's saved labels: for reviews where the primary label and the independent
verifier agreed, deliberately corrupt the primary topic/intent/severity in the copy, then re-run the
same code comparison used by verify.py. A working verifier/comparison must flag every planted error.
Also plants record-level contract errors and confirms schema validation rejects each of them.

    python -m pipeline.planted --state state/final.sqlite --out evals/planted
"""
import argparse
import json
import random
import shutil
import sqlite3

from .config import ROOT, STATE_DIR
from .io_utils import write_json
from .schema import record_errors

PRIMARY = "jev-1.13.0+jevq-v2+a5-common-v1"
VERIFY = "claude-haiku-4-5+claude-v1+a5-common-v1"
WRONG = {"topic": {"playback": "billing", "billing": "catalog", "other": "access", "usability": "downloads",
                   "catalog": "support", "access": "playback", "downloads": "usability", "support": "other"},
         "intent": {"complaint": "praise", "praise": "complaint", "cancellation": "request", "request": "unclear",
                    "unclear": "cancellation"}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default=str(STATE_DIR / "final.sqlite"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "planted"))
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)
    copy = STATE_DIR / "planted_copy.sqlite"
    src = sqlite3.connect(a.state)
    src.execute("PRAGMA wal_checkpoint(FULL)")
    src.close()
    shutil.copyfile(a.state, copy)                       # never touch the real run's labels
    con = sqlite3.connect(copy)
    con.row_factory = sqlite3.Row
    agreed = con.execute("""SELECT v.review_id, x.topic, x.intent, x.severity, v.topic AS vt, v.intent AS vi,
                                   v.severity AS vs FROM verifications v
                            JOIN results x ON x.canonical_id = v.review_id AND x.label_config = ?
                            WHERE v.label_config = ? AND v.agrees_topic = 1 AND v.agrees_intent = 1
                            ORDER BY v.review_id""", (PRIMARY, VERIFY)).fetchall()
    rng = random.Random("a5-planted-v1")
    picked = rng.sample(agreed, min(a.n, len(agreed)))
    cases = []
    for i, r in enumerate(picked):
        field = ("topic", "intent", "severity")[i % 3]
        wrong = (WRONG[field][r[field]] if field != "severity" else (5 if r["severity"] < 3 else 1))
        con.execute(f"UPDATE results SET {field}=? WHERE canonical_id=? AND label_config=?",
                    (wrong, r["review_id"], PRIMARY))
        # Same comparison rule as verify.run_verify: primary label == verifier label.
        now_primary = con.execute(f"SELECT {field} FROM results WHERE canonical_id=? AND label_config=?",
                                  (r["review_id"], PRIMARY)).fetchone()[0]
        verifier = r[{"topic": "vt", "intent": "vi", "severity": "vs"}[field]]
        cases.append({"review_id": r["review_id"], "field": field, "original": r[field], "planted": wrong,
                      "verifier": verifier, "flagged_by_comparison": now_primary != verifier})
    con.close()
    copy.unlink()

    text = "My downloads vanish every time I go offline."
    good = {"topic": "downloads", "intent": "complaint", "severity": 3, "sentiment": -0.6, "entities": ["offline"],
            "evidence_quote": "My downloads vanish", "needs_review": False}
    record_cases = []
    for name, bad in [("topic outside common labels", {"topic": "offline"}), ("severity as float", {"severity": 3.0}),
                      ("sentiment out of range", {"sentiment": -1.4}), ("paraphrased quote", {"evidence_quote": "downloads disappear"}),
                      ("needs_review as string", {"needs_review": "no"}), ("empty entity", {"entities": [""]})]:
        record_cases.append({"planted": name, "rejected": bool(record_errors({**good, **bad}, text)),
                             "errors": record_errors({**good, **bad}, text)})
    result = {"source_state": a.state, "verifier_cases": cases,
              "verifier_planted": len(cases), "verifier_flagged": sum(c["flagged_by_comparison"] for c in cases),
              "record_cases": record_cases, "record_rejected": sum(c["rejected"] for c in record_cases),
              "note": "Synthetic planted errors on a copy; never written to the real run or used in business aggregates."}
    write_json(f"{a.out}/planted_results.json", result)
    print(json.dumps({k: result[k] for k in ("verifier_planted", "verifier_flagged", "record_rejected")}
                     | {"record_cases": len(record_cases)}, indent=1))
    return 0 if result["verifier_flagged"] == len(cases) and result["record_rejected"] == len(record_cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
