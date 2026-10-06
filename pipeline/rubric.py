"""Shared label definitions (GRADING_CONTRACT.md) and the deterministic parts of enrichment.

Kept in one place so the Jev questions, the Claude prompts and the code rules all use the same
wording. Contains NO golden answers and no examples drawn from the golden 50.
"""
import re

TOPIC_DEFS = {
    "access": "Login, signup, password or account access",
    "usability": "Navigation, controls, layout, queue/playlist management, ad interruptions",
    "playback": "Playback failure, crashes, lag, connection failures, audio quality, resource use",
    "downloads": "Downloading, saved music, offline listening, disappearing downloads",
    "catalog": "Missing songs/artists, search/discovery, recommendations, lyrics availability",
    "billing": "Price, charges, subscriptions, paywalls, premium entitlement; explicitly premium-only controls",
    "support": "Contacting support and the support response",
    "other": "General praise or criticism, unrelated content, or no supported specific topic",
}
TOPIC_RULES = ("Choose the problem with the highest severity; on a tie, the first specific problem mentioned. "
               "For a positive review choose the first specific praised feature; general praise is other. "
               "Mentioning a paid plan alone is not billing; a subscription failing to activate is billing; "
               "music crashing for a paying customer is playback.")

INTENT_DEFS = {
    "cancellation": "Explicitly leaving, uninstalling, cancelling, or threatening to do so",
    "complaint": "Negative experience, including mixed praise and criticism; general 'bad app' counts",
    "request": "Asks for a change or feature without reporting a failure",
    "praise": "Positive experience only",
    "unclear": "Bare boycott slogans, unrelated or meaningless text, no product complaint or personal departure",
}
INTENT_RULES = ("Precedence: cancellation, then complaint, then request, then praise, then unclear. "
                "Pick the first that applies.")

SEVERITY_LEVELS = [
    "1: No reported problem: praise, neutral or unclear content, or a pure feature request",
    "2: Dislike, generic criticism, minor annoyance or cosmetic issue; no functional loss described",
    "3: A degraded or restricted function; some use or a workaround remains",
    "4: A clearly blocked core task, such as being unable to log in or play music",
    "5: Explicit serious financial, privacy or data harm (an expensive plan, a crash or angry words alone are not enough)",
]

SENTIMENT_LEVELS = ["very negative", "somewhat negative", "neutral or mixed", "somewhat positive", "very positive"]
SENTIMENT_VALUES = [-1.0, -0.5, 0.0, 0.5, 1.0]

# Feature terms matched in code for `entities`. Canonical name -> regex (word-boundary, case-insensitive).
ENTITY_TERMS = {
    "ads": r"\bads?\b|advert", "login": r"log ?in|sign ?in|logged out", "password": r"password",
    "account": r"\baccount", "signup": r"sign ?up|register", "shuffle": r"shuffle", "queue": r"\bqueue",
    "playlist": r"playlist", "search": r"\bsearch", "lyrics": r"lyric", "recommendations": r"recommend",
    "podcast": r"podcast", "downloads": r"download", "offline": r"offline", "premium": r"premium",
    "subscription": r"subscri", "price": r"\bprice|\bcost|expensive|charg", "refund": r"refund",
    "crash": r"crash", "lag": r"\blag|freez|slow", "skip": r"\bskip", "bluetooth": r"bluetooth",
    "car": r"android auto|carplay|\bcar\b", "widget": r"widget", "notifications": r"notification",
    "battery": r"battery", "audio quality": r"audio quality|sound quality", "support": r"customer service|\bsupport",
    "update": r"\bupdate", "connection": r"connect|internet|wifi|wi-fi", "smart shuffle": r"smart shuffle",
    "dj": r"\bdj\b", "wrapped": r"wrapped",
}
_ENTITY_RE = {k: re.compile(v, re.I) for k, v in ENTITY_TERMS.items()}


def entities(text: str) -> list[str]:
    """Explicitly matched feature terms, in a stable order. Deterministic; no model involved."""
    return [k for k, rx in _ENTITY_RE.items() if rx.search(text)]


_SENT_RE = re.compile(r"[^.!?\n]+[.!?]*")


def sentence_candidates(text: str, max_candidates: int = 12) -> list[str]:
    """Exact substrings of the review (whitespace-trimmed spans), usable as evidence quotes."""
    out = []
    for m in _SENT_RE.finditer(text):
        s = m.group().strip()
        if len(s) >= 3 and s not in out:
            out.append(s)
        if len(out) >= max_candidates:
            break
    return out


def short_enough_for_full_quote(text: str, limit: int = 160) -> bool:
    return len(text.strip()) <= limit or len(sentence_candidates(text)) <= 1


def apply_rubric_rules(intent: str, severity: int) -> int:
    """Shared severity table as code: no reported problem => 1; a complaint is at least 2."""
    if intent in ("praise", "request", "unclear"):
        return 1
    if intent == "complaint" and severity < 2:
        return 2
    return severity
