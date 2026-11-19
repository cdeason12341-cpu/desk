#!/usr/bin/env python3
"""Restore a settings preset into desk.db.

  PYTHONPATH=. .venv/bin/python scripts/restore_preset.py data/presets/strat_b_2026-11-19.json

PROTECTED keys only take effect in a desk process that calls presets.apply itself
(force=True on its Settings object), or after a desk.service restart. This CLI writes
the db; a running desk will hot-load only non-protected keys. Never prints secrets.
"""
import argparse, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config, db, presets

def main():
    p = argparse.ArgumentParser()
    p.add_argument("preset")
    p.add_argument("--db", default=str(ROOT / "desk.db"))
    p.add_argument("--source", default="cli: restore_preset")
    args = p.parse_args()
    s = config.Settings(db.DB(args.db))
    changed = presets.apply(s, args.preset, source=args.source, force=True)
    print("applied", args.preset)
    print("changed", ", ".join(changed) if changed else "(none)")
    prot = [k for k in changed if k in config.PROTECTED]
    if prot:
        print("protected keys changed:", ", ".join(prot))
        print("if the desk is already running, either let it apply this itself or restart desk.service")

if __name__ == "__main__":
    main()
