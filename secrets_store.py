"""Reads secrets from .secrets/ files. Never prints or logs values."""
from pathlib import Path
DIR = Path(__file__).resolve().parent / ".secrets"


def get(name: str, default: str | None = None) -> str | None:
    p = DIR / name
    try:
        v = p.read_text().strip()
        return v or default
    except FileNotFoundError:
        return default


def all_values() -> list[str]:
    out = []
    for p in DIR.glob("*"):
        try:
            v = p.read_text().strip()
            if len(v) >= 8:
                out.append(v)
        except Exception:
            pass
    return out


def redact(text: str) -> str:
    for v in all_values():
        text = text.replace(v, "<redacted>")
    return text
