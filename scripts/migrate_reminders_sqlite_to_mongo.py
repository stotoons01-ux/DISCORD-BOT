"""One-shot migration script: SQLite `reminders.db` -> MongoDB collection.

Run after setting MONGO_URI in env. This script upserts by (user_id, created_at, message)
to make it idempotent.
"""
import os
import sys
import sqlite3
from pathlib import Path
from pymongo import UpdateOne

# Ensure the repository root (parent of DISCORD BOT) is on sys.path so imports like
# `from mongo_client_wrapper import get_mongo_client` work when this script is run
# directly from other directories or from CI.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mongo_client_wrapper import get_mongo_client

SQLITE_PATH = str(Path(__file__).resolve().parents[0] / '..' / 'reminders.db')
DB_NAME = os.getenv('MONGO_DB_NAME', 'reminder_db')
COLLECTION_NAME = os.getenv('MONGO_REMINDERS_COLLECTION', 'reminders')


def migrate(sqlite_path=SQLITE_PATH, batch_size=500):
    if not os.path.exists(sqlite_path):
        print('SQLite DB not found at:', sqlite_path)
        return

    client = get_mongo_client()
    col = client[DB_NAME][COLLECTION_NAME]

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM reminders')
    rows = cursor.fetchall()

    ops = []
    count = 0
    for row in rows:
        doc = {
            'user_id': row['user_id'],
            'channel_id': row['channel_id'],
            'guild_id': row['guild_id'],
            'message': row['message'],
            'reminder_time': row['reminder_time'],
            'created_at': row['created_at'],
            'is_active': bool(row['is_active']),
            'is_sent': bool(row['is_sent']),
            'is_recurring': bool(row['is_recurring']) if 'is_recurring' in row.keys() else False,
            'recurrence_type': row['recurrence_type'] if 'recurrence_type' in row.keys() else None,
            'recurrence_interval': row['recurrence_interval'] if 'recurrence_interval' in row.keys() else None,
            'original_time_pattern': row['original_time_pattern'] if 'original_time_pattern' in row.keys() else None,
            'mention': row['mention'] if 'mention' in row.keys() else 'everyone',
        }

        filter_query = {'user_id': doc['user_id'], 'created_at': doc['created_at'], 'message': doc['message']}
        ops.append(UpdateOne(filter_query, {'$setOnInsert': doc}, upsert=True))
        count += 1

        if len(ops) >= batch_size:
            col.bulk_write(ops)
            ops = []

    if ops:
        col.bulk_write(ops)

    print(f'Migrated/processed {count} rows.')
    conn.close()


if __name__ == '__main__':
    migrate()
