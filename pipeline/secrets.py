"""Load API keys from the local .env into the process environment. Never prints or logs values."""
import os

from dotenv import load_dotenv

from .config import ROOT


def require(name: str) -> str:
    load_dotenv(ROOT / ".env", override=False)
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Add it to the local .env file (see .env.example).")
    return value


def redact(text: str) -> str:
    """Remove anything key-shaped from an error message before it is logged."""
    import re
    return re.sub(r"(apikey_|sk-ant-|sk-)[A-Za-z0-9_-]{8,}", r"\1[REDACTED]", str(text))
