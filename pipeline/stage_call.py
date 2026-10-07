"""One bounded Claude call for the group/memo stages, cached by a hash of its exact inputs.

A warm run with unchanged inputs, model and prompt version makes no call: the saved artifact is
reused and nothing new is logged (cached reuse is not a new API call).
"""
import hashlib
import json
import time

from .calls import CallRecord, Usage, log_call
from .db import transaction
from .ledger import now
from .secrets import redact


def cache_key(role, model, prompt_version, payload) -> tuple[str, str]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_sha = hashlib.sha256(body.encode()).hexdigest()
    return hashlib.sha256(f"{role}|{model}|{prompt_version}|{input_sha}".encode()).hexdigest(), input_sha


class NotCached(LookupError):
    """Raised when allow_call=False and no saved artifact matches these exact inputs."""


def cached_json_call(con, *, role, run_id, ledger, model, prompt_version, system, payload, schema, max_tokens,
                     review_ids=(), est_cost=0.05, allow_call=True):
    """Returns (output_dict, request_id, cached: bool). Raises RuntimeError on a failed call (logged).
    With allow_call=False it never touches the network: a cache miss raises NotCached."""
    import anthropic

    from .backends.claude import cost_of
    from .secrets import require
    key, input_sha = cache_key(role, model, prompt_version, payload)
    hit = con.execute("SELECT output, request_id FROM artifacts WHERE cache_key=?", (key,)).fetchone()
    if hit:
        return json.loads(hit["output"]), hit["request_id"], True
    if not allow_call:
        raise NotCached(f"{role}/{prompt_version}: no saved output for these inputs")
    client = anthropic.Anthropic(api_key=require("ANTHROPIC_API_KEY"), max_retries=0, timeout=180.0)
    res = ledger.reserve(est_cost)
    started, t = now(), time.perf_counter()
    base = dict(run_id=run_id, role=role, phase=None, provider="anthropic", model=model,
                settings={"thinking": "off (not requested)", "max_tokens": max_tokens, "prompt_version": prompt_version,
                          "input_sha256": input_sha},
                label_config=None, review_ids=list(review_ids), started_at=started)
    try:
        msg = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": schema}})
    except anthropic.APIError as e:
        err = redact(e)
        with transaction(con):
            log_call(con, CallRecord(request_id=getattr(e, "request_id", None) or f"{role}-failed-{started}",
                                     outcome="failed", error=err[:500], ended_at=now(),
                                     latency_s=round(time.perf_counter() - t, 4), **base))
        ledger.settle(res, None)
        raise RuntimeError(f"{role} call failed: {err}")
    u = msg.usage
    usage = Usage(u.input_tokens, u.output_tokens, u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0)
    usage.cost_usd = cost_of(model, usage)
    ok = msg.stop_reason == "end_turn"
    out = json.loads(next(b.text for b in msg.content if b.type == "text")) if ok else None
    with transaction(con):
        log_call(con, CallRecord(request_id=msg._request_id, outcome="succeeded" if ok else "failed", usage=usage,
                                 error=None if ok else f"stop_reason={msg.stop_reason}", ended_at=now(),
                                 latency_s=round(time.perf_counter() - t, 4),
                                 extra={"stop_reason": msg.stop_reason, "served_model": msg.model}, **base))
        if ok:
            con.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                        (key, role, input_sha, msg._request_id, json.dumps(out, ensure_ascii=False), now()))
    ledger.settle(res, usage.cost_usd, msg._request_id)
    if not ok:
        raise RuntimeError(f"{role} call stopped with {msg.stop_reason}")
    return out, msg._request_id, False
