"""Named settings presets. No secrets. Restore applies in-process with force=True so PROTECTED keys take effect in the running desk without a restart."""
from __future__ import annotations
import json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRESET_DIR = ROOT / "data" / "presets"
STANDARD_B = PRESET_DIR / "strat_b_2026-11-19.json"

# Never read or write these from a preset file.
SECRET_KEYS = {
    "desk.rpc_url", "desk.rpc", "gmgn.api_key", "gmgn.signing_key",
}
SECRET_SUB = ("token", "password", "secret", "private", "rpc_url", "api_key", "signing")


def _forbidden(key: str) -> bool:
    k = (key or "").lower()
    if k in SECRET_KEYS:
        return True
    return any(p in k for p in SECRET_SUB)


def load(path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    data = json.loads(p.read_text())
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("preset settings must be an object")
    cleaned = {}
    for k, v in settings.items():
        if not isinstance(k, str) or _forbidden(k):
            continue
        cleaned[k] = v
    data["settings"] = cleaned
    data["_path"] = str(p)
    return data


def apply(s, path, *, source: str, force: bool = True, overrides: dict | None = None) -> list[str]:
    """Apply a preset to a live Settings object (in-memory + db + change_log).
    force=True is required so PROTECTED keys (mode, wallet shares, caps) update this process."""
    import config
    data = load(path)
    keys = dict(data["settings"])
    if overrides:
        keys.update(overrides)
    changed = []
    for k, v in keys.items():
        if _forbidden(k):
            continue
        try:
            config.resolve(k)
        except Exception:
            continue
        try:
            old = s[k]
        except Exception:
            old = None
        if old == v or (isinstance(old, float) and isinstance(v, (int, float)) and float(old) == float(v)):
            continue
        s.set(k, v, source=source, force=force)
        changed.append(k)
    return changed


def restore_standard_b(s, *, source: str, enabled: bool = True, mode: str = "LIVE") -> list[str]:
    """Restore the 2026-11-19 standard B preset and put B on that profile.
    Always sets profile to default. enabled/mode default to LIVE+on so a kill switch can resume standard B.
    Open GTA positions are not touched here (their row profile stays 'gta')."""
    return apply(s, STANDARD_B, source=source, force=True, overrides={
        "strat_b.profile": "default",
        "strat_b.enabled": bool(enabled),
        "strat_b.mode": mode,
    })
