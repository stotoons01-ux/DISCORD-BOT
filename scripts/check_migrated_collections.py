"""Check migrated collections: print counts and sample docs for each migrated collection.

Usage:
  python check_migrated_collections.py
"""
import os
import json
from pprint import pprint

from mongo_client_wrapper import get_mongo_client


def main():
    uri = os.getenv('MONGO_URI')
    if not uri:
        print('MONGO_URI not set; cannot check migrations')
        return 2
    db_name = os.getenv('MONGO_DB_NAME', 'discord_bot')
    client = get_mongo_client(uri)
    db = client[db_name]

    collections = [
        'user_timezones',
        'birthdays',
        'user_profiles',
        'giftcode_state',
    ]

    for coll in collections:
        c = db[coll]
        try:
            count = c.count_documents({})
        except Exception as e:
            print(f'Failed to count documents in {coll}: {e}')
            count = 'error'
        print(f'Collection: {coll} — count: {count}')
        try:
            sample = c.find_one({})
            if sample:
                # remove _id for nicer printing
                sample2 = dict(sample)
                sample2.pop('_id', None)
                print(' Sample doc:')
                pprint(sample2)
            else:
                print(' No sample (collection empty)')
        except Exception as e:
            print(f' Failed to fetch sample for {coll}: {e}')

    return 0


if __name__ == '__main__':
    exit(main())
