"""Record validation, batch-ID validation and planted-error handling."""
from pipeline.schema import record_errors, rubric_warnings, validate_batch

TEXT = "Can't log in after the update."
GOOD = {"topic": "access", "intent": "complaint", "severity": 4, "sentiment": -0.6, "entities": ["login"],
        "evidence_quote": "Can't log in", "needs_review": False}


def test_valid_record_passes():
    assert record_errors(GOOD, TEXT) == []


def test_each_planted_error_is_caught():
    planted = {
        "topic": "login", "intent": "angry", "severity": 4.0, "sentiment": 1.5,
        "entities": ["", "x"], "evidence_quote": "cannot log in", "needs_review": "false",
    }
    for field, bad in planted.items():
        assert record_errors({**GOOD, field: bad}, TEXT), f"planted {field}={bad!r} not caught"
    assert record_errors({**GOOD, "severity": True}, TEXT), "bool is not an int severity"
    assert record_errors({**GOOD, "sentiment": float("nan")}, TEXT)


def test_quote_must_be_exact_substring_no_normalisation():
    text = "Café “quotes”  double  space"
    assert record_errors({**GOOD, "evidence_quote": "Café “quotes”"}, text) == []
    assert record_errors({**GOOD, "evidence_quote": 'Cafe "quotes"'}, text)
    assert record_errors({**GOOD, "evidence_quote": "double space"}, text)


def test_batch_rejects_unknown_duplicate_and_missing_ids():
    sent = {"a": TEXT, "b": TEXT, "c": TEXT}
    returned = [{"review_id": "a", **GOOD}, {"review_id": "b", **GOOD}, {"review_id": "b", **GOOD},
                {"review_id": "zzz-injected", **GOOD}]
    valid, rejected = validate_batch(sent, returned)
    assert set(valid) == {"a"}
    assert "b" in rejected and "c" in rejected
    assert any(k.startswith("<unknown") for k in rejected)


def test_non_list_response_rejects_everything():
    valid, rejected = validate_batch({"a": TEXT}, {"review_id": "a"})
    assert valid == {} and rejected == {"a": "id missing from response"}


def test_rubric_warnings():
    assert rubric_warnings({"intent": "praise", "severity": 3})
    assert rubric_warnings({"intent": "complaint", "severity": 1})
    assert rubric_warnings({"intent": "complaint", "severity": 5})
    assert not rubric_warnings({"intent": "cancellation", "severity": 1})
