import os
from datetime import datetime
from typing import List, Dict, Optional
from bson import ObjectId
from pymongo import ASCENDING
from pymongo.errors import PyMongoError

from mongo_client_wrapper import get_mongo_client

DB_NAME = os.getenv('MONGO_DB_NAME', 'reminder_db')
COLLECTION_NAME = os.getenv('MONGO_REMINDERS_COLLECTION', 'reminders')


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


class ReminderStorageMongo:
    """Mongo-backed replacement for ReminderStorage (SQLite).

    Provides the same method names used by the bot code: add_reminder, get_due_reminders,
    get_user_reminders, mark_reminder_sent, delete_reminder, get_all_active_reminders.
    Documents will include an `id` field (string of ObjectId) to match existing expectations.
    """

    def __init__(self, client=None):
        self.client = client or get_mongo_client()
        self.db = self.client[DB_NAME]
        self.col = self.db[COLLECTION_NAME]
        # Indexes for common queries
        try:
            self.col.create_index([('is_active', ASCENDING), ('is_sent', ASCENDING), ('reminder_time', ASCENDING)])
            self.col.create_index('user_id')
            self.col.create_index('channel_id')
        except Exception:
            pass

    def _normalize_doc(self, doc: dict) -> dict:
        # Convert Mongo document to shape bot expects
        if not doc:
            return doc
        doc['id'] = str(doc.get('_id'))
        # Convert stored ISO strings back to datetimes
        try:
            if isinstance(doc.get('reminder_time'), str):
                doc['reminder_time'] = datetime.fromisoformat(doc['reminder_time'])
        except Exception:
            pass
        try:
            if isinstance(doc.get('created_at'), str):
                doc['created_at'] = datetime.fromisoformat(doc['created_at'])
        except Exception:
            pass
        return doc

    def add_reminder(self, user_id: str, channel_id: str, guild_id: str, message: str, reminder_time: datetime,
                     is_recurring: bool = False, recurrence_type: Optional[str] = None,
                     recurrence_interval: Optional[int] = None, original_pattern: Optional[str] = None,
                     mention: str = 'everyone', image_url: Optional[str] = None, thumbnail_url: Optional[str] = None,
                     author_name: Optional[str] = None, author_icon_url: Optional[str] = None,
                     footer_text: Optional[str] = None, footer_icon_url: Optional[str] = None) -> str:
        try:
            # Deduplicate: if an active, unsent reminder for the same user/channel/time/message exists,
            # update it with provided metadata instead of inserting a new document.
            filt = {
                'user_id': user_id,
                'channel_id': channel_id,
                'reminder_time': _to_iso(reminder_time),
                'message': message,
                'is_active': True,
                'is_sent': False
            }
            existing = self.col.find_one(filt)
            if existing:
                updates = {}
                if image_url is not None:
                    updates['image_url'] = image_url
                if thumbnail_url is not None:
                    updates['thumbnail_url'] = thumbnail_url
                if author_name is not None:
                    updates['author_name'] = author_name
                if author_icon_url is not None:
                    updates['author_icon_url'] = author_icon_url
                if footer_text is not None:
                    updates['footer_text'] = footer_text
                if footer_icon_url is not None:
                    updates['footer_icon_url'] = footer_icon_url
                if mention is not None:
                    updates['mention'] = mention

                if updates:
                    self.col.update_one({'_id': existing['_id']}, {'$set': updates})
                    return str(existing['_id'])
                return str(existing['_id'])

            doc = {
                'user_id': user_id,
                'channel_id': channel_id,
                'guild_id': guild_id,
                'message': message,
                'reminder_time': _to_iso(reminder_time),
                'created_at': _to_iso(datetime.utcnow()),
                'is_active': True,
                'is_sent': False,
                'is_recurring': bool(is_recurring),
                'recurrence_type': recurrence_type,
                'recurrence_interval': recurrence_interval,
                'original_time_pattern': original_pattern,
                'mention': mention,
                'image_url': image_url,
                'thumbnail_url': thumbnail_url,
                'author_name': author_name,
                'author_icon_url': author_icon_url,
                'footer_text': footer_text,
                'footer_icon_url': footer_icon_url,
            }
            res = self.col.insert_one(doc)
            return str(res.inserted_id)
        except PyMongoError:
            return -1

    def _find_cursor(self, filter_query, sort=None, limit: Optional[int] = None):
        cursor = self.col.find(filter_query)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        return cursor

    def get_due_reminders(self) -> List[Dict]:
        try:
            now_iso = datetime.utcnow().isoformat()
            cursor = self._find_cursor({'is_active': True, 'is_sent': False, 'reminder_time': {'$lte': now_iso}}, sort=[('reminder_time', ASCENDING)])
            out = []
            for d in cursor:
                out.append(self._normalize_doc(d))
            return out
        except PyMongoError:
            return []

    def get_user_reminders(self, user_id: str, limit: int = 10) -> List[Dict]:
        try:
            cursor = self._find_cursor({'user_id': user_id, 'is_active': True, 'is_sent': False}, sort=[('reminder_time', ASCENDING)], limit=limit)
            out = [self._normalize_doc(d) for d in cursor]
            return out
        except PyMongoError:
            return []

    def mark_reminder_sent(self, reminder_id) -> bool:
        try:
            # Accept string id or ObjectId
            try:
                oid = ObjectId(reminder_id)
                filt = {'_id': oid}
            except Exception:
                filt = {'_id': reminder_id}
            res = self.col.update_one(filt, {'$set': {'is_sent': True}})
            return res.modified_count > 0
        except PyMongoError:
            return False

    def update_reminder_fields(self, reminder_id, fields: dict) -> bool:
        try:
            try:
                oid = ObjectId(reminder_id)
                filt = {'_id': oid}
            except Exception:
                filt = {'_id': reminder_id}
            # Only set keys present in fields
            update = {'$set': {k: v for k, v in fields.items()}}
            res = self.col.update_one(filt, update)
            return res.modified_count > 0
        except PyMongoError:
            return False

    def delete_reminder(self, reminder_id, user_id: str) -> bool:
        try:
            try:
                oid = ObjectId(reminder_id)
                filt = {'_id': oid, 'user_id': user_id, 'is_active': True}
            except Exception:
                filt = {'_id': reminder_id, 'user_id': user_id, 'is_active': True}
            res = self.col.update_one(filt, {'$set': {'is_active': False}})
            return res.modified_count > 0
        except PyMongoError:
            return False

    def get_all_active_reminders(self) -> List[Dict]:
        try:
            cursor = self._find_cursor({'is_active': True, 'is_sent': False}, sort=[('reminder_time', ASCENDING)])
            return [self._normalize_doc(d) for d in cursor]
        except PyMongoError:
            return []

    def update_reminder_time(self, reminder_id, next_time: datetime) -> bool:
        try:
            try:
                oid = ObjectId(reminder_id)
                filt = {'_id': oid}
            except Exception:
                filt = {'_id': reminder_id}
            res = self.col.update_one(filt, {'$set': {'reminder_time': _to_iso(next_time), 'is_sent': False}})
            return res.modified_count > 0
        except PyMongoError:
            return False
