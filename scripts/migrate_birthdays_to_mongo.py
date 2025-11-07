"""Migrate `birthdays.json` -> MongoDB (idempotent upserts).

Usage:
  python migrate_birthdays_to_mongo.py [--dry-run]
"""
import json
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / 'birthdays.json'

def main(dry_run: bool = True):
    if not FILE.exists():
        print(f"No file found at {FILE}; nothing to migrate")
        return 0

    with FILE.open('r', encoding='utf-8') as f:
        data = json.load(f)

    sys.path.insert(0, str(ROOT))
    from mongo_adapters import mongo_enabled, BirthdaysAdapter

    print(f"Found {len(data)} birthday entries in {FILE}")
    if not mongo_enabled() and not dry_run:
        print("MONGO_URI not set — aborting (use environment variable to enable)")
        return 2

    if dry_run:
        print("Dry-run: would upsert the following user IDs:")
        for uid in list(data.keys())[:50]:
            print(' -', uid, '->', data[uid])
        if len(data) > 50:
            print(f"... and {len(data)-50} more")
        return 0

    count = 0
    for uid, val in data.items():
        try:
            day = int(val.get('day'))
            month = int(val.get('month'))
        except Exception:
            print(f"Skipping invalid entry for {uid}: {val}")
            continue
        ok = BirthdaysAdapter.set(str(uid), day, month)
        if ok:
            count += 1

    print(f"Migrated {count} birthday entries to Mongo")
    return 0


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    sys.exit(main(dry_run=args.dry_run))
