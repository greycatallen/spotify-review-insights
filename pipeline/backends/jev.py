"""Jev (TypeSafe System One) enrichment backend.

One HTTP request per review: Jev judges one state per request, and all questions about that
review (topic, intent, severity, sentiment and, for long reviews, the evidence sentence) are
asked together so they run in parallel against the same state. The review text is the only
state; stars and other metadata are never sent.

Billing: input tokens only ($/Mtok in cost/rates.csv); output is free. The SDK's own retries are
disabled so every attempt is visible in our call log and spend ledger.
"""
from ..calls import Usage
from ..enrich import Backend, BackendError, BackendResult
from ..rubric import (INTENT_DEFS, INTENT_RULES, SENTIMENT_LEVELS, SENTIMENT_VALUES, SEVERITY_LEVELS, TOPIC_DEFS,
                      TOPIC_RULES, apply_rubric_rules, entities, sentence_candidates, short_enough_for_full_quote)
from ..secrets import redact, require

PROMPT_VERSION = "jevq-v2"
SCHEMA_VERSION = "a5-common-v1"

# v2: same contract meaning as v1 in fewer tokens (smoke test measured ~570 tokens for a request with
# the v1 topic question alone and ~130 per extra question). Wording is still taken from the contract.
V2_TOPICS = {
    "access": "login, signup, password, account access",
    "usability": "navigation, controls, layout, queue/playlist management, ad interruptions",
    "playback": "playback failure, crashes, lag, connection, audio quality, battery/data use",
    "downloads": "downloading, saved/offline music, disappearing downloads",
    "catalog": "missing songs/artists, search, discovery, recommendations, lyrics",
    "billing": "price, charges, subscriptions, paywalls, premium entitlement or premium-only controls",
    "support": "contacting support, support response",
    "other": "general praise/criticism, unrelated, or no specific topic",
}
V2_INTENTS = {
    "cancellation": "leaving, uninstalling, cancelling, or threatening to",
    "complaint": "negative experience, incl. mixed praise and criticism",
    "request": "wants a change, no failure reported",
    "praise": "positive only",
    "unclear": "slogan, unrelated or meaningless",
}
V2_SEVERITY = ["no problem (praise, unclear, pure request)", "dislike or minor annoyance, nothing broken",
               "a function degraded or restricted, workaround remains", "a core task blocked (cannot log in or play)",
               "explicit serious money, privacy or data harm"]
V2_SENTIMENT = ["very negative", "negative", "neutral or mixed", "positive", "very positive"]


def build_questions(text: str, version: str = PROMPT_VERSION):
    """Question dicts for one review. Returns (questions, quote_candidates or None)."""
    from typesafe_sdk import Choice, Score
    if version == "jevq-v2":
        q = {
            "topic": Choice(instructions="Product area of the review's most severe problem (if tied, the first "
                                         "mentioned; if positive, the first praised feature). Mentioning a paid "
                                         "plan alone is not billing.", criteria=V2_TOPICS),
            "intent": Choice(instructions="Reviewer intent; first that applies in this order.", criteria=V2_INTENTS),
            "severity": Score(instructions="Severity of the problem as stated in the text.", criteria=V2_SEVERITY),
            "sentiment": Score(instructions="Sentiment of the review.", criteria=V2_SENTIMENT),
        }
        cands = None
        if not short_enough_for_full_quote(text):
            cands = sentence_candidates(text)
            q["quote"] = Choice(instructions="Sentence that best evidences the main problem or point.",
                                criteria={f"s{i}": s for i, s in enumerate(cands)})
        return q, cands
    q = {
        "topic": Choice(
            instructions={"task": "Which product area is this app-store review about?", "rules": TOPIC_RULES},
            criteria=TOPIC_DEFS),
        "intent": Choice(
            instructions={"task": "What is the reviewer's intent?", "rules": INTENT_RULES},
            criteria=INTENT_DEFS),
        "severity": Score(
            instructions="How severe is the problem the review itself describes? Judge only what the text says; "
                         "do not infer impact that is not stated.",
            criteria=SEVERITY_LEVELS),
        "sentiment": Score(instructions="Overall sentiment of the review text.", criteria=SENTIMENT_LEVELS),
    }
    cands = None
    if not short_enough_for_full_quote(text):
        cands = sentence_candidates(text)
        q["quote"] = Choice(
            instructions="Which sentence from the review is the best evidence for its main problem "
                         "(or, if it reports no problem, its main point)?",
            criteria={f"s{i}": s for i, s in enumerate(cands)})
    return q, cands


def _level_index(answer):
    """Most probable rubric level as a 0-based index, independent of how the API keys levels."""
    keys = sorted(answer.probabilities)
    best = max(keys, key=lambda k: answer.probabilities[k])
    return keys.index(best), keys


def to_record(review_id: str, text: str, resp, cands, needs_review_threshold: float) -> dict:
    topic, intent = resp.choices["topic"], resp.choices["intent"]
    sev_idx, _ = _level_index(resp.scores["severity"])
    sent = resp.scores["sentiment"]
    skeys = sorted(sent.probabilities)
    sentiment = round(sum(sent.probabilities[k] * SENTIMENT_VALUES[i] for i, k in enumerate(skeys)), 3)
    severity = apply_rubric_rules(intent.choice, sev_idx + 1)
    if cands:
        quote = cands[int(resp.choices["quote"].choice[1:])]
    else:
        quote = text.strip()
    low_conf = min(topic.confidence, intent.confidence) < needs_review_threshold
    return {"review_id": review_id, "topic": topic.choice, "intent": intent.choice, "severity": severity,
            "sentiment": max(-1.0, min(1.0, sentiment)), "entities": entities(text), "evidence_quote": quote,
            "needs_review": bool(low_conf),
            "_probs": {"topic": dict(topic.probabilities), "intent": dict(intent.probabilities),
                       "severity": {str(k): v for k, v in resp.scores["severity"].probabilities.items()},
                       "topic_conf": topic.confidence, "intent_conf": intent.confidence}}


class JevBackend(Backend):
    provider = "typesafe"
    route = "primary"

    def __init__(self, model="jev-1.13.0", price_per_mtok=0.042, timeout=30.0, needs_review_threshold=0.35,
                 est_tokens_per_review=1500, prompt_version=PROMPT_VERSION):
        from typesafe_sdk import RetryPolicy, TypeSafeClient
        self.model = model
        self.price = price_per_mtok / 1e6
        self.threshold = needs_review_threshold
        self.est_tokens = est_tokens_per_review
        self.prompt_version = prompt_version
        self.label_config = f"{model}+{prompt_version}+{SCHEMA_VERSION}"
        self.settings = {"thinking": "n/a (System One: no generated text)", "reviews_per_request": 1,
                         "sdk_retries": 0, "timeout_s": timeout, "needs_review_conf_lt": needs_review_threshold,
                         "prompt_version": prompt_version}
        self.client = TypeSafeClient(api_key=require("TYPESAFE_API_KEY"), model=model, timeout=timeout,
                                     retry=RetryPolicy(max_retries=0))

    def estimate_tokens(self, items):
        return sum(self.est_tokens + 2 * len(it["text"]) // 3 for it in items)

    def estimate_cost(self, items):
        # Worst case for the reservation: generous token estimate x input price (output is free).
        return self.estimate_tokens(items) * self.price

    def classify(self, items):
        from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError, TypeSafeAPITimeoutError
        if len(items) != 1:
            raise ValueError("Jev backend sends exactly one review per request (use --batch-size 1)")
        it = items[0]
        questions, cands = build_questions(it["text"], self.prompt_version)
        try:
            resp = self.client.system_one(state={"review": it["text"]}, questions=questions)
        except TypeSafeAPITimeoutError as e:
            raise BackendError(redact(e), sent=True, uncertain=True)
        except TypeSafeAPIError as e:
            # 4xx validation/auth errors are not retryable; 429/5xx are.
            raise BackendError(redact(e), request_id=e.request_id, sent=True,
                               retryable=e.status in (408, 429) or e.status >= 500,
                               usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0) if e.status < 500 else Usage())
        except TypeSafeAPIConnectionError as e:
            raise BackendError(redact(e), sent=False)
        tokens = resp.usage.input_tokens
        rec = to_record(it["review_id"], it["text"], resp, cands, self.threshold)
        probs = rec.pop("_probs")
        return BackendResult(
            request_id=resp.request_id or f"jev-noid-{it['review_id']}",
            records=[rec],
            usage=Usage(input_tokens=tokens, output_tokens=resp.usage.output_tokens,
                        cost_usd=tokens * self.price if tokens is not None else None),
            extra={"served_model": resp.model, "probabilities": probs, "quote_candidates": len(cands or [])})
