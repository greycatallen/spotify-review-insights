"""Paths, shared label vocabulary and the course checker module.

Nothing here reads API keys; see pipeline/secrets.py.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Final Assignment - Spotify Reviews Dataset"
FULL_CSV = DATA_DIR / "spotify_reviews_18months.csv"
COST_100_CSV = DATA_DIR / "cost_100.csv"
CHECKPOINT_500_CSV = DATA_DIR / "checkpoint_500.csv"
ANALYSIS_10000_CSV = DATA_DIR / "analysis_10000.csv"
GOLDEN_CSV = DATA_DIR / "golden_50_to_label.csv"
MANIFEST = DATA_DIR / "manifest.json"
CHECKER_PATH = DATA_DIR / "check_submission.py"

STATE_DIR = ROOT / "state"
GRADING_DIR = ROOT / "grading"
COST_DIR = ROOT / "cost"
GOLDEN_DIR = ROOT / "golden"


def _load_checker():
    # Reuse the provided helpers (row_sha, csv_rows, profile, mean_string) so hashes and
    # rounding match the grader byte for byte instead of re-implementing them.
    spec = importlib.util.spec_from_file_location("a5_checker", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

AUDIT_VERSION = checker.VERSION
FIELDS = checker.FIELDS
TOPICS = checker.TOPICS
INTENTS = checker.INTENTS
# Intent precedence from GRADING_CONTRACT.md (first applicable wins).
INTENT_PRECEDENCE = ("cancellation", "complaint", "request", "praise", "unclear")
COMPLAINT_INTENTS = ("complaint", "cancellation")
EMPTY_REASON = "empty_review_text"
