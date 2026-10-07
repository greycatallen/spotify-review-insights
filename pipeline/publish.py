"""Publish a run's saved results into the dashboard database (SQLite locally, Neon Postgres when deployed).

Everything shown on the dashboard is computed here from saved records, membership and AI artifacts;
the backend only reads. Default is offline: AI-generated parts (request-theme names, recommendations)
are taken from the run's input-hash cache. `--execute` allows the two bounded Haiku calls if missing.

    python -m pipeline.publish --state state/dev_500_w1.sqlite --db sqlite:///state/dashboard.sqlite
    python -m pipeline.publish --state ... --db "$DATABASE_URL" --execute
"""
import argparse
import json
from collections import Counter

from sqlalchemy import (JSON, Boolean, Column, Float, Integer, MetaData, String, Table, Text, create_engine,
                        insert)

from .config import COMPLAINT_INTENTS
from .db import connect
from .export import iter_records
from .group import name_request_themes, request_themes
from .ledger import SpendLedger, now
from .memo import build_claims
from .rank import compute_ranking
from .recommend import evidence_pack, recommend
from .stage_call import NotCached

PRIMARY_CONFIG = "jev-1.13.0+jevq-v2+a5-common-v1"
FALLBACK_CONFIG = "claude-haiku-4-5+claude-v1+a5-common-v1"
VERIFY_CONFIG = FALLBACK_CONFIG

meta = MetaData()
T_KV = Table("run_summary", meta, Column("key", String(64), primary_key=True), Column("value", JSON))
T_REVIEWS = Table(
    "reviews", meta, Column("review_id", String(64), primary_key=True), Column("review_date", String(10)),
    Column("month", String(7), index=True), Column("rating", Integer), Column("likes", Integer),
    Column("app_version", String(32)), Column("status", String(16), index=True), Column("reason", Text),
    Column("topic", String(16), index=True), Column("intent", String(16), index=True), Column("severity", Integer),
    Column("sentiment", Float), Column("evidence_quote", Text), Column("review_text", Text),
    Column("needs_review", Boolean), Column("issue_id", String(64), index=True), Column("theme_id", String(64), index=True),
    Column("cache_source_id", String(64)), Column("label_config", String(96)))
T_ISSUES = Table(
    "issues", meta, Column("issue_id", String(64), primary_key=True), Column("kind", String(16)),
    Column("topic", String(16)), Column("name", Text), Column("description", Text), Column("rank", Integer),
    Column("complaint_count", Integer), Column("severity_sum", Integer), Column("mean_severity", String(16)),
    Column("priority_score", Integer), Column("cancellation_count", Integer))
T_CLAIMS = Table("claims", meta, Column("claim_id", String(16), primary_key=True), Column("issue_id", String(64)),
                 Column("metric", String(32)), Column("value", String(32)))
T_RECS = Table("recommendations", meta, Column("rec_id", Integer, primary_key=True), Column("payload", JSON))
T_AGG = Table("aggregates", meta, Column("id", Integer, primary_key=True, autoincrement=True),
              Column("name", String(32), index=True), Column("k1", String(32)), Column("k2", String(32)),
              Column("value", Integer))


def build(con, label_configs, *, allow_call=False, run_label="", log=print):
    records = list(iter_records(con, label_configs))
    src = {r["review_id"]: dict(r) for r in con.execute(
        "SELECT review_id, review_text, review_rating, review_likes, app_version, review_timestamp, src_order "
        "FROM reviews")}
    done = [r for r in records if r["status"] == "completed"]
    sev = {r["review_id"]: r["severity"] for r in done}
    members = {}
    for m in con.execute("SELECT issue_id, review_id FROM membership"):
        members.setdefault(m["issue_id"], []).append(m["review_id"])
    issue_of = {rid: iid for iid, ids in members.items() for rid in ids}
    ranking = compute_ranking((iid, rid, sev[rid]) for iid, ids in members.items() for rid in ids)
    names = {r["issue_id"]: dict(r) for r in con.execute("SELECT issue_id, name, description FROM issues")}
    for r in ranking:
        r["name"] = names.get(r["issue_id"], {}).get("name") or r["issue_id"]
    claims = build_claims(ranking, top_n=8)
    themes = request_themes(con, label_configs)
    theme_of = {rid: t for t, ids in themes.items() for rid in ids}
    ledger = SpendLedger(con, 1.0, f"publish-{now()}")
    try:
        theme_names, theme_info = name_request_themes(con, themes, label_configs, run_id=ledger.run_id, ledger=ledger,
                                                      allow_call=allow_call)
    except NotCached as e:
        theme_names, theme_info = {}, {"pending": str(e)}
    quotes = {r["review_id"]: r["evidence_quote"] for r in done}

    # Run-level context: every number the AI may quote without a claim ID.
    intents = Counter(r["intent"] for r in done)
    vr = con.execute("""SELECT count(*), sum(agrees_topic), sum(agrees_intent), sum(agrees_severity)
                        FROM verifications WHERE label_config=?""", (VERIFY_CONFIG,)).fetchone()
    in_scope = [r for r in records if r.get("reason") != "not_in_scope"]
    context = {"reviews_in_scope": len(in_scope), "completed": len(done),
               "quarantined": sum(r["status"] == "quarantined" for r in in_scope),
               "complaints": intents["complaint"], "cancellation_intent": intents["cancellation"],
               "requests": intents["request"], "praise": intents["praise"], "unclear": intents["unclear"],
               "verification_sample": vr[0], "issues_ranked": len(ranking)}
    pack = evidence_pack(ranking, claims, members, quotes, themes, theme_names, context)
    try:
        rec = recommend(con, pack, claims, run_id=ledger.run_id, ledger=ledger, allow_call=allow_call)
    except NotCached as e:
        rec = {"pending": str(e)}

    cancel_by_issue = Counter(issue_of[r["review_id"]] for r in done if r["intent"] == "cancellation")
    issue_rows = [{"issue_id": r["issue_id"], "kind": "complaint", "topic": r["issue_id"].split(".")[0],
                   "name": r["name"], "description": names.get(r["issue_id"], {}).get("description"),
                   "rank": r["rank"], "complaint_count": r["complaint_count"], "severity_sum": r["severity_sum"],
                   "mean_severity": r["mean_severity"], "priority_score": r["priority_score"],
                   "cancellation_count": cancel_by_issue[r["issue_id"]]} for r in ranking]
    for i, t in enumerate(sorted(themes, key=lambda t: (-len(themes[t]), t)), 1):
        issue_rows.append({"issue_id": t, "kind": "request", "topic": t[4:].split(".")[0],
                           "name": theme_names.get(t, {}).get("name") or t,
                           "description": theme_names.get(t, {}).get("description"), "rank": i,
                           "complaint_count": len(themes[t]), "severity_sum": None, "mean_severity": None,
                           "priority_score": None, "cancellation_count": 0})

    review_rows = []
    for r in records:
        s = src[r["review_id"]]
        row = {"review_id": r["review_id"], "review_date": s["review_timestamp"][:10], "month": s["review_timestamp"][:7],
               "rating": int(s["review_rating"]) if s["review_rating"].isdigit() else None,
               "likes": int(s["review_likes"]) if s["review_likes"].isdigit() else None,
               "app_version": s["app_version"] or None, "status": r["status"], "reason": r.get("reason"),
               "topic": None, "intent": None, "severity": None, "sentiment": None, "evidence_quote": None,
               "review_text": None, "needs_review": None, "issue_id": None, "theme_id": None,
               "cache_source_id": None, "label_config": None}
        if r["status"] == "completed":
            row.update({k: r.get(k) for k in ("topic", "intent", "severity", "sentiment", "evidence_quote",
                                              "needs_review", "cache_source_id", "label_config")})
            row["review_text"] = s["review_text"][:2000]
            row["issue_id"], row["theme_id"] = issue_of.get(r["review_id"]), theme_of.get(r["review_id"])
        review_rows.append(row)

    agg = Counter()
    for r in done:
        s = src[r["review_id"]]
        agg[("intent", r["intent"], "")] += 1
        agg[("topic", r["topic"], "")] += 1
        agg[("severity", str(r["severity"]), "")] += 1
        agg[("sentiment_bin", f"{min(int((r['sentiment'] + 1) / 0.25), 7)}", "")] += 1
        agg[("month_intent", s["review_timestamp"][:7], r["intent"])] += 1
        agg[("rating_intent", s["review_rating"], r["intent"])] += 1
        agg[("topic_intent", r["topic"], r["intent"])] += 1
        if r["intent"] in COMPLAINT_INTENTS:
            agg[("complaint_topic_month", s["review_timestamp"][:7], r["topic"])] += 1
    for r in records:
        agg[("status", r["status"], r.get("reason", "") if r["status"] == "quarantined" else "")] += 1

    calls = con.execute("SELECT role, count(*) AS n, sum(cost_usd) AS usd, sum(input_tokens) AS tin, "
                        "sum(output_tokens) AS tout, sum(outcome='failed') AS failed FROM calls GROUP BY role").fetchall()
    summary = {
        "run": {"label": run_label, "published_at": now(), "label_configs": label_configs,
                "input_sha256": con.execute("SELECT value FROM meta WHERE key='input_sha256'").fetchone()[0]},
        "counts": {"source_rows": len(records), "in_scope": len(in_scope), "completed": len(done),
                   "quarantined_in_scope": context["quarantined"],
                   "not_in_scope": sum(r.get("reason") == "not_in_scope" for r in records),
                   "cache_reuses": sum("cache_source_id" in r for r in done),
                   "needs_review": sum(bool(r["needs_review"]) for r in done),
                   "complaint_or_cancellation": sum(r["intent"] in COMPLAINT_INTENTS for r in done)},
        "verification": {"compared": vr[0], "topic_agreement": round(vr[1] / vr[0], 4) if vr[0] else None,
                         "intent_agreement": round(vr[2] / vr[0], 4) if vr[0] else None,
                         "severity_agreement": round(vr[3] / vr[0], 4) if vr[0] else None},
        "calls": {c["role"]: {"requests": c["n"], "failed": c["failed"], "cost_usd": round(c["usd"] or 0, 6),
                              "input_tokens": c["tin"], "output_tokens": c["tout"]} for c in calls},
        "ai": {"request_themes": theme_info, "recommendations": {k: v for k, v in rec.items() if k != "output"}},
        "context": context,
    }
    summary["cost_usd_total"] = round(sum(v["cost_usd"] for v in summary["calls"].values()), 6)
    return {"summary": summary, "reviews": review_rows, "issues": issue_rows, "claims": claims,
            "recommendations": rec.get("output"), "aggregates": agg, "pack": pack}


def write_db(url, data, chunk=5000):
    engine = create_engine(url, future=True)
    meta.drop_all(engine)
    meta.create_all(engine)
    with engine.begin() as cx:
        cx.execute(insert(T_KV), [{"key": k, "value": v} for k, v in data["summary"].items()])
        for i in range(0, len(data["reviews"]), chunk):
            cx.execute(insert(T_REVIEWS), data["reviews"][i:i + chunk])
        if data["issues"]:
            cx.execute(insert(T_ISSUES), data["issues"])
        if data["claims"]:
            cx.execute(insert(T_CLAIMS), [dict(zip(("claim_id", "issue_id", "metric", "value"), c)) for c in data["claims"]])
        if data["recommendations"]:
            cx.execute(insert(T_RECS), [{"rec_id": 1, "payload": data["recommendations"]}])
        cx.execute(insert(T_AGG), [{"name": n, "k1": a, "k2": b, "value": v} for (n, a, b), v in data["aggregates"].items()])
    return engine


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True, help="pipeline state file of the run to publish")
    ap.add_argument("--db", default="sqlite:///state/dashboard.sqlite", help="SQLAlchemy URL of the dashboard DB")
    ap.add_argument("--label", default="", help="human label for this run, shown on the dashboard")
    ap.add_argument("--execute", action="store_true", help="allow the 2 bounded Haiku calls if not already cached")
    a = ap.parse_args(argv)
    con = connect(a.state)
    data = build(con, [PRIMARY_CONFIG, FALLBACK_CONFIG], allow_call=a.execute, run_label=a.label)
    write_db(a.db.replace("postgres://", "postgresql+psycopg://").replace("postgresql://", "postgresql+psycopg://"), data)
    s = data["summary"]
    print(json.dumps({"counts": s["counts"], "issues": len(data["issues"]), "ai": s["ai"],
                      "cost_usd_total": s["cost_usd_total"]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
