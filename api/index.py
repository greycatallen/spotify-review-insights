"""Read-only dashboard backend: serves processed results from the database (Neon Postgres when
deployed, SQLite locally). It never calls a model and never writes; publishing is pipeline/publish.py.

Local:   DATABASE_URL=sqlite:///state/dashboard.sqlite .venv/bin/uvicorn api.index:app --reload
Vercel:  api/index.py is the serverless entry point; DATABASE_URL is a project environment variable.
"""
import json
import os
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent


def _url():
    url = os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'state' / 'dashboard.sqlite'}")
    return url.replace("postgres://", "postgresql+psycopg://").replace("postgresql://", "postgresql+psycopg://")


# NullPool: serverless invocations should not hold idle connections open against Neon.
engine = create_engine(_url(), poolclass=NullPool, future=True)
app = FastAPI(title="Spotify review insights API", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


def rows(sql, **params):
    with engine.connect() as cx:
        return [dict(r._mapping) for r in cx.execute(text(sql), params)]


def _j(v):
    """JSON columns come back as text on SQLite and as objects on Postgres."""
    return json.loads(v) if isinstance(v, str) else v


@app.get("/api/health")
def health():
    n = rows("SELECT count(*) AS n FROM reviews")[0]["n"]
    return {"ok": True, "reviews": n, "database": engine.dialect.name}


@app.get("/api/summary")
def summary():
    return {r["key"]: _j(r["value"]) for r in rows("SELECT key, value FROM run_summary")}


@app.get("/api/distributions")
def distributions():
    out = defaultdict(list)
    for r in rows("SELECT name, k1, k2, value FROM aggregates ORDER BY name, k1, k2"):
        out[r["name"]].append({"k1": r["k1"], "k2": r["k2"], "value": r["value"]})
    return out


@app.get("/api/issues")
def issues(kind: str = Query("complaint", pattern="^(complaint|request)$"), limit: int = Query(50, le=500)):
    return rows("SELECT * FROM issues WHERE kind = :kind ORDER BY rank LIMIT :limit", kind=kind, limit=limit)


@app.get("/api/issues/{issue_id}")
def issue_detail(issue_id: str, limit: int = Query(25, le=200), offset: int = Query(0, ge=0)):
    found = rows("SELECT * FROM issues WHERE issue_id = :i", i=issue_id)
    if not found:
        raise HTTPException(404, "unknown issue_id")
    col = "theme_id" if found[0]["kind"] == "request" else "issue_id"
    members = rows(f"""SELECT review_id, review_date, rating, intent, severity, sentiment, evidence_quote,
                              needs_review, cache_source_id
                       FROM reviews WHERE {col} = :i ORDER BY severity DESC, review_date DESC, review_id
                       LIMIT :limit OFFSET :offset""", i=issue_id, limit=limit, offset=offset)
    claims = rows("SELECT claim_id, metric, value FROM claims WHERE issue_id = :i ORDER BY claim_id", i=issue_id)
    return {"issue": found[0], "claims": claims, "members": members, "member_count": found[0]["complaint_count"]}


@app.get("/api/claims")
def claims():
    return rows("SELECT * FROM claims")


@app.get("/api/recommendations")
def recommendations():
    payload = rows("SELECT payload FROM recommendations WHERE rec_id = 1")
    ai = {r["key"]: _j(r["value"]) for r in rows("SELECT key, value FROM run_summary WHERE key = 'ai'")}.get("ai", {})
    return {"recommendations": _j(payload[0]["payload"]) if payload else None, "provenance": ai.get("recommendations")}


@app.get("/api/reviews/{review_id}")
def review(review_id: str):
    found = rows("SELECT * FROM reviews WHERE review_id = :r", r=review_id)
    if not found:
        raise HTTPException(404, "unknown review_id")
    return found[0]


@app.get("/api/reviews")
def search_reviews(topic: str | None = None, intent: str | None = None, q: str | None = Query(None, max_length=80),
                   limit: int = Query(25, le=100)):
    where, params = ["status = 'completed'"], {"limit": limit}
    if topic:
        where.append("topic = :topic"); params["topic"] = topic
    if intent:
        where.append("intent = :intent"); params["intent"] = intent
    if q:
        where.append("lower(review_text) LIKE :q"); params["q"] = f"%{q.lower()}%"
    return rows(f"""SELECT review_id, review_date, rating, topic, intent, severity, evidence_quote, issue_id, theme_id
                    FROM reviews WHERE {' AND '.join(where)} ORDER BY review_date DESC, review_id LIMIT :limit""",
                **params)


# Local development: serve the static dashboard from the same process (Vercel serves /dashboard statically).
@app.get("/")
def index():
    return FileResponse(ROOT / "dashboard" / "index.html")
