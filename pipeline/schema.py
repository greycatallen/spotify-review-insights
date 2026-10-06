"""Validation of enrichment output against the grading contract, applied before anything is saved.

Mirrors check_submission.py's record rules and adds batch-level checks (every returned ID must be
one we sent, exactly once) so a bad model response can never silently mislabel or drop reviews.
"""
import math

from .config import INTENTS, TOPICS

LABEL_FIELDS = ("topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review")


def record_errors(rec: dict, source_text: str) -> list[str]:
    """Return a list of contract violations for one labelled record (empty list = valid)."""
    errs = []
    if rec.get("topic") not in TOPICS:
        errs.append(f"topic {rec.get('topic')!r} not in common labels")
    if rec.get("intent") not in INTENTS:
        errs.append(f"intent {rec.get('intent')!r} not in common labels")
    sev = rec.get("severity")
    if type(sev) is not int or not 1 <= sev <= 5:
        errs.append(f"severity {sev!r} must be int 1-5")
    s = rec.get("sentiment")
    if type(s) not in (int, float) or not math.isfinite(s) or not -1 <= s <= 1:
        errs.append(f"sentiment {s!r} must be a finite number in [-1, 1]")
    if type(rec.get("needs_review")) is not bool:
        errs.append("needs_review must be boolean")
    ents = rec.get("entities")
    if not isinstance(ents, list) or not all(isinstance(e, str) and e.strip() for e in ents):
        errs.append("entities must be a list of nonempty strings")
    q = rec.get("evidence_quote")
    if not isinstance(q, str) or not q.strip():
        errs.append("evidence_quote must be a nonempty string")
    elif q not in source_text:
        errs.append("evidence_quote is not an exact substring of the review text")
    return errs


def rubric_warnings(rec: dict) -> list[str]:
    """Soft consistency checks from the shared severity table. These route to needs_review/verify,
    they do not reject: the rubric allows e.g. a cancellation with no reported problem (severity 1)."""
    w = []
    intent, sev = rec.get("intent"), rec.get("severity")
    if intent in ("praise", "request", "unclear") and sev != 1:
        w.append(f"{intent} with severity {sev} (rubric: no reported problem = 1)")
    if intent == "complaint" and sev == 1:
        w.append("complaint with severity 1 (rubric: complaints are at least 2)")
    if sev == 5:
        w.append("severity 5 requires explicit financial/privacy/data harm")
    return w


def validate_batch(sent: dict[str, str], returned: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """Split a model batch response into (valid records by id, rejected ids -> reason).

    `sent` maps review_id -> exact text actually sent. Unknown IDs are ignored (and reported),
    duplicated IDs are rejected entirely, and missing IDs are rejected so they stay pending.
    """
    valid, rejected, seen = {}, {}, {}
    for item in returned if isinstance(returned, list) else []:
        rid = item.get("review_id") if isinstance(item, dict) else None
        if rid not in sent:
            rejected[f"<unknown:{rid!r}>"[:80]] = "returned id was not in this request"
            continue
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:
            valid.pop(rid, None)
            rejected[rid] = "id returned more than once"
            continue
        errs = record_errors(item, sent[rid])
        if errs:
            rejected[rid] = "; ".join(errs)
        else:
            valid[rid] = {k: item[k] for k in LABEL_FIELDS} | (
                {"subtopic": item["subtopic"]} if isinstance(item.get("subtopic"), str) else {})
    for rid in sent:
        if rid not in seen:
            rejected[rid] = "id missing from response"
    return valid, rejected
