"""Migrate `giftcode_state.json` -> MongoDB (single document upsert).

Usage:
  python migrate_giftcode_state_to_mongo.py [--dry-run]
"""
import json
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / 'giftcode_state.json'

def main(dry_run: bool = True):
    if not FILE.exists():
        print(f"No file found at {FILE}; nothing to migrate")
        return 0

    with FILE.open('r', encoding='utf-8') as f:
        data = json.load(f)

    sys.path.insert(0, str(ROOT))
    from mongo_adapters import mongo_enabled, GiftcodeStateAdapter

    print(f"Found giftcode_state.json with keys: {list(data.keys())}")
    if not mongo_enabled() and not dry_run:
        print("MONGO_URI not set — aborting (use environment variable to enable)")
        return 2

    if dry_run:
        print("Dry-run: would upsert giftcode_state document (preview):")
        for k, v in list(data.items()):
            print(f" - {k}: {type(v).__name__}")
        return 0

    ok = GiftcodeStateAdapter.set_state(data)
    print("Migrated giftcode_state:" , "success" if ok else "failed")
    return 0 if ok else 3


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    sys.exit(main(dry_run=args.dry_run))
