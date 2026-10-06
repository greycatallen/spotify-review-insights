"""Keys stay local: .env is git-ignored and no submitted artifact contains a key-shaped string."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_PATTERN = re.compile(r"apikey_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}")
SCANNED = ("grading", "cost", "pipeline", "tests", "golden", "README.md", ".env.example")


def test_env_is_git_ignored():
    out = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == ".env"
    tracked = subprocess.run(["git", "ls-files", "--", ".env", ".env.*"], cwd=ROOT, capture_output=True, text=True)
    assert tracked.stdout.split() in ([], [".env.example"])


def test_env_example_is_blank():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line:
            assert line.split("=", 1)[1].strip() == "", line.split("=")[0]


def test_no_key_shaped_strings_in_artifacts():
    hits = []
    for name in SCANNED:
        p = ROOT / name
        files = [p] if p.is_file() else (p.rglob("*") if p.exists() else [])
        for f in files:
            if f.is_file() and f.suffix not in (".gz", ".sqlite", ".pyc") and f.name != "test_secrets.py":
                if KEY_PATTERN.search(f.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(f.relative_to(ROOT)))
    assert hits == []
