import os, sqlite3, json
from pathlib import Path
from pymongo import MongoClient

MONGO_URI = os.getenv('MONGO_URI')
MONGO_DB = os.getenv('MONGO_DB_NAME','reminderbot')
MONGO_COL = os.getenv('MONGO_REMINDERS_COLLECTION','reminders')

# SQLite check
p = Path('reminders.db')
if p.exists():
    conn = sqlite3.connect(str(p)); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM reminders")
        print("SQLite count:", cur.fetchone()[0])
    except Exception as e:
        print("SQLite error:", e)
    conn.close()
else:
    print("SQLite: reminders.db not found")

# Mongo check
if MONGO_URI:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    col = client[MONGO_DB][MONGO_COL]
    print("Mongo count:", col.count_documents({}))
    print("Mongo sample (latest 3):")
    for d in col.find().sort([('_id', -1)]).limit(3):
        print({k:(str(v) if k=='_id' else v) for k,v in d.items()})
else:
    print("MONGO_URI not set in environment")
