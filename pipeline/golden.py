"""Validate the human-labelled golden 50 and convert it to check_submission.py's benchmark format.

The labels are human answers: they are only ever read here, for scoring. No prompt builder
imports this module.

    python -m pipeline.golden golden/golden_50_labeled.csv --reviewer "<your name>"
"""
import argparse
import json
import sys
from pathlib import Path

from .config import GOLDEN_CSV, GOLDEN_DIR, INTENTS, TOPICS, checker
from .schema import record_errors


def _parse(row):
    def multi(v):  # allow "playback|usability" for genuinely ambiguous cases
        return [x.strip() for x in v.split("|") if x.strip()]
    ents = row.get("entities", "").strip() or "[]"
    rec = {
        "topic": multi(row.get("topic", "")),
        "intent": multi(row.get("intent", "")),
        "severity": [int(x) for x in multi(row.get("severity", "")) if x.isdigit()],
        "sentiment": row.get("sentiment", "").strip(),
        "entities": ents,
        "evidence_quote": row.get("evidence_quote", ""),
        "needs_review": row.get("needs_review", "").strip().lower(),
    }
    return rec


def validate(path):
    originals = {r["review_id"]: r for r in checker.csv_rows(GOLDEN_CSV)}
    problems, cases = [], []
    rows = list(checker.csv_rows(path))
    ids = [r["review_id"] for r in rows]
    if sorted(ids) != sorted(originals):
        problems.append(f"IDs differ from golden_50_to_label.csv ({len(ids)} rows)")
    for row in rows:
        rid = row["review_id"]
        if rid in originals and row["review_text"] != originals[rid]["review_text"]:
            problems.append(f"{rid}: review_text was altered (spreadsheet re-encoding?)")
        p = _parse(row)
        errs = []
        if not p["topic"] or any(t not in TOPICS for t in p["topic"]):
            errs.append(f"topic {p['topic']}")
        if not p["intent"] or any(i not in INTENTS for i in p["intent"]):
            errs.append(f"intent {p['intent']}")
        if not p["severity"] or any(not 1 <= s <= 5 for s in p["severity"]):
            errs.append(f"severity {row.get('severity')!r}")
        try:
            sentiment = float(p["sentiment"])
            entities = json.loads(p["entities"])
        except ValueError as e:
            errs.append(f"sentiment/entities not parseable: {e}")
        else:
            single = {"topic": p["topic"][0] if p["topic"] else None, "intent": p["intent"][0] if p["intent"] else None,
                      "severity": p["severity"][0] if p["severity"] else None, "sentiment": sentiment,
                      "entities": entities, "evidence_quote": p["evidence_quote"],
                      "needs_review": {"true": True, "false": False}.get(p["needs_review"])}
            if not single["evidence_quote"].strip():
                single["evidence_quote"] = originals.get(rid, row)["review_text"]   # human quote is optional
            errs += [e for e in record_errors(single, originals.get(rid, row)["review_text"])
                     if not e.startswith(("topic", "intent", "severity"))]
        if errs:
            problems.append(f"{rid}: " + "; ".join(errs))
        cases.append({"review_id": rid, "status": "approved", "accepted_topic": p["topic"],
                      "accepted_intent": p["intent"], "accepted_severity": p["severity"]})
    return problems, cases


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("labeled_csv", type=Path)
    ap.add_argument("--reviewer", required=True, help="human labeller's name, stored in the benchmark file")
    ap.add_argument("--out", type=Path, default=GOLDEN_DIR / "gold.json")
    a = ap.parse_args(argv)
    problems, cases = validate(a.labeled_csv)
    for c in cases:
        c["reviewer"] = a.reviewer
    if problems:
        print(f"{len(problems)} problem(s):")
        print("\n".join("  - " + p for p in problems))
        return 1
    checker.write_json(a.out, {"version": "student-golden-50-v1", "cases": cases})
    print(f"OK: {len(cases)} cases valid; wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
