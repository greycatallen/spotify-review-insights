"""Model-call log: every attempt (success or failure) for every role is one row in `calls`.

Backends report usage exactly as the provider returned it. Missing usage stays NULL (shown as
unavailable), it is never filled with an estimate.
"""
import json
from dataclasses import dataclass, field

from .ledger import now

ROLES = ("enrich", "verify", "group", "memo")


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    billed_units: float | None = None      # e.g. Jev classifications, if billed per unit
    billed_unit: str | None = None
    cost_usd: float | None = None          # from cost.rates at call time; None = unknown price


@dataclass
class CallRecord:
    request_id: str
    run_id: str
    role: str
    provider: str
    model: str
    settings: dict
    review_ids: list
    outcome: str
    started_at: str
    ended_at: str
    latency_s: float
    phase: str | None = None
    label_config: str | None = None
    attempt: int = 1
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    extra: dict = field(default_factory=dict)


def log_call(con, c: CallRecord):
    """Insert one call row. Call inside the same transaction that saves its results."""
    if c.role not in ROLES:
        raise ValueError(f"unknown role {c.role}")
    if c.outcome not in ("succeeded", "failed"):
        raise ValueError(f"unknown outcome {c.outcome}")
    u = c.usage
    con.execute(
        """INSERT INTO calls(request_id, run_id, role, phase, provider, model, settings, label_config, review_ids,
               outcome, attempt, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, billed_units,
               billed_unit, cost_usd, started_at, ended_at, latency_s, error, extra)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (c.request_id, c.run_id, c.role, c.phase, c.provider, c.model, json.dumps(c.settings, sort_keys=True),
         c.label_config, json.dumps(c.review_ids), c.outcome, c.attempt, u.input_tokens, u.output_tokens,
         u.cache_read_tokens, u.cache_write_tokens, u.billed_units, u.billed_unit, u.cost_usd,
         c.started_at, c.ended_at, c.latency_s, c.error, json.dumps(c.extra, sort_keys=True, ensure_ascii=False)))


def local_request_id(prefix: str) -> str:
    """For backends that return no provider request id. Unique, sortable, never reused."""
    import uuid
    return f"local-{prefix}-{now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:12]}"
