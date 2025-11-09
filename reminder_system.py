import json
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Union
import re
from pathlib import Path
import discord
from discord.ext import commands, tasks
import logging
import pytz
import threading
import os
try:
    from mongo_adapters import mongo_enabled, UserTimezonesAdapter
except Exception:
    # If adapters are not available at import time, functions will fall back to file-based storage
    mongo_enabled = lambda: False
    UserTimezonesAdapter = None

USER_TZ_FILE = Path(__file__).with_name('user_timezones.json')


def _load_user_timezones() -> dict:
    try:
        # Prefer Mongo if configured
        if mongo_enabled():
            try:
                return UserTimezonesAdapter.load_all() or {}
            except Exception:
                # fallback to file if adapter fails
                pass
        if USER_TZ_FILE.exists():
            with USER_TZ_FILE.open('r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load user timezones: {e}")
    return {}


def _save_user_timezones(data: dict):
    try:
        # Prefer Mongo when available
        if mongo_enabled():
            try:
                # write per-user upserts
                for uid, tz in (data or {}).items():
                    UserTimezonesAdapter.set(str(uid), str(tz))
                return
            except Exception:
                # fallback to file if adapter fails
                pass
        with USER_TZ_FILE.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save user timezones: {e}")


_user_tz_lock = threading.Lock()


def get_user_timezone(user_id: Union[int, str]) -> Optional[str]:
    try:
        with _user_tz_lock:
            # If Mongo is enabled, use adapter directly for single lookup
            if mongo_enabled() and UserTimezonesAdapter is not None:
                try:
                    return UserTimezonesAdapter.get(str(user_id))
                except Exception:
                    pass
            data = _load_user_timezones()
            return data.get(str(user_id))
    except Exception:
        return None


def set_user_timezone(user_id: Union[int, str], tz_abbr: str) -> bool:
    try:
        with _user_tz_lock:
            # If Mongo is enabled, write using adapter
            if mongo_enabled() and UserTimezonesAdapter is not None:
                try:
                    return UserTimezonesAdapter.set(str(user_id), tz_abbr)
                except Exception:
                    pass
            data = _load_user_timezones()
            data[str(user_id)] = tz_abbr.lower()
            _save_user_timezones(data)
        return True
    except Exception as e:
        logger.error(f"Failed to set user timezone for {user_id}: {e}")
        return False

logger = logging.getLogger(__name__)

# Reminder image URLs - synced with images.js
REMINDER_IMAGES = {
    'set': 'https://i.postimg.cc/Fzq03CJf/a463d7c7-7fc7-47fc-b24d-1324383ee2ff-removebg-preview.png',  # Logo when setting a reminder
    'alert': 'https://cdn.discordapp.com/attachments/1424082091750068364/1431246283334619136/Gemini_Generated_Image_dqdcnxdqdcnxdqdc-removebg-preview.png?ex=68fcb779&is=68fb65f9&hm=6afcbee2e3dea071f560ea5e1615dc24a691a1e12e123b02cb3c7f0cba2a33db'  # Logo when receiving a reminder
}

def get_accurate_utc_time() -> datetime:
    """
    Get accurate UTC time using the system clock (now synchronized with NTP).
    This returns UTC time which can be converted to any timezone as needed.
    """
    # Use datetime.utcnow() which is the correct way to get UTC time
    # This automatically handles the timezone conversion properly
    utc_time = datetime.utcnow()
    
    logger.debug(f"✅ Using NTP-synchronized UTC time: {utc_time}")
    return utc_time

def get_current_time_in_timezone(timezone_abbr: str) -> datetime:
    """
    Get current time in the specified timezone.
    
    Args:
        timezone_abbr: Timezone abbreviation (e.g., 'ist', 'utc', 'est')
        
    Returns:
        datetime: Current time in the specified timezone (naive)
    """
    utc_time = get_accurate_utc_time()
    
    if not timezone_abbr or timezone_abbr.lower() not in TimeParser.TIMEZONE_MAP:
        return utc_time  # Return UTC if no valid timezone specified
    
    # Convert UTC to target timezone
    tz_name = TimeParser.TIMEZONE_MAP[timezone_abbr.lower()]
    target_tz = pytz.timezone(tz_name)
    utc_tz = pytz.UTC
    
    # Convert UTC to target timezone
    utc_aware = utc_tz.localize(utc_time)
    target_time = utc_aware.astimezone(target_tz).replace(tzinfo=None)
    
    logger.debug(f"✅ Current time in {timezone_abbr.upper()}: {target_time}")
    return target_time

class ReminderStorage:
    """Handles storage and retrieval of reminders using SQLite database"""
    
    def __init__(self, db_path: str = "reminders.db"):
        self.db_path = Path(db_path)
        self.init_database()
    
    def init_database(self):
        """Initialize the SQLite database with reminders table including recurring support"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Create main reminders table with recurring support
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        guild_id TEXT,
                        message TEXT NOT NULL,
                        reminder_time TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        is_sent INTEGER DEFAULT 0,
                        is_recurring INTEGER DEFAULT 0,
                        recurrence_type TEXT DEFAULT NULL,
                        recurrence_interval INTEGER DEFAULT NULL,
                        original_time_pattern TEXT DEFAULT NULL,
                        mention TEXT DEFAULT 'everyone',
                        image_url TEXT DEFAULT NULL,
                        thumbnail_url TEXT DEFAULT NULL,
                        author_name TEXT DEFAULT NULL,
                        author_icon_url TEXT DEFAULT NULL,
                        footer_text TEXT DEFAULT NULL,
                        footer_icon_url TEXT DEFAULT NULL
                    )
                ''')

                # Add new columns to existing table if they don't exist
                try:
                    cursor.execute('ALTER TABLE reminders ADD COLUMN is_recurring INTEGER DEFAULT 0')
                except sqlite3.OperationalError:
                    pass  # Column already exists

                try:
                    cursor.execute('ALTER TABLE reminders ADD COLUMN recurrence_type TEXT DEFAULT NULL')
                except sqlite3.OperationalError:
                    pass  # Column already exists

                try:
                    cursor.execute('ALTER TABLE reminders ADD COLUMN recurrence_interval INTEGER DEFAULT NULL')
                except sqlite3.OperationalError:
                    pass  # Column already exists

                try:
                    cursor.execute('ALTER TABLE reminders ADD COLUMN original_time_pattern TEXT DEFAULT NULL')
                except sqlite3.OperationalError:
                    pass  # Column already exists

                try:
                    cursor.execute('ALTER TABLE reminders ADD COLUMN mention TEXT DEFAULT \'everyone\'')
                except sqlite3.OperationalError:
                    pass  # Column already exists

                try:
                    cursor.execute("ALTER TABLE reminders ADD COLUMN image_url TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass  # Column already exists

                try:
                    cursor.execute("ALTER TABLE reminders ADD COLUMN thumbnail_url TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass

                try:
                    cursor.execute("ALTER TABLE reminders ADD COLUMN author_name TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass

                try:
                    cursor.execute("ALTER TABLE reminders ADD COLUMN author_icon_url TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass

                try:
                    cursor.execute("ALTER TABLE reminders ADD COLUMN footer_text TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass

                try:
                    cursor.execute("ALTER TABLE reminders ADD COLUMN footer_icon_url TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass

                conn.commit()
                logger.info("✅ Reminder database initialized successfully with recurring support")
        except sqlite3.DatabaseError as e:
            if "file is not a database" in str(e).lower():
                logger.warning(f"Invalid database file detected: {e}. Deleting and recreating...")
                try:
                    self.db_path.unlink(missing_ok=True)
                    logger.info("✅ Deleted invalid database file. Retrying initialization...")
                except Exception as del_e:
                    logger.error(f"Failed to delete invalid database file: {del_e}")
                    return  # Can't proceed
                # Retry initialization with fresh database
                self.init_database()
            else:
                logger.error(f"❌ Database error during initialization: {e}")
        except Exception as e:
            logger.error(f"❌ Failed to initialize reminder database: {e}")
    
    def add_reminder(self, user_id: str, channel_id: str, guild_id: str, message: str, reminder_time: datetime,
                    is_recurring: bool = False, recurrence_type: str = None, recurrence_interval: int = None,
                    original_pattern: str = None, mention: str = 'everyone', image_url: str = None,
                    thumbnail_url: str = None, author_name: str = None, author_icon_url: str = None,
                    footer_text: str = None, footer_icon_url: str = None) -> int:
        """Add a new reminder to the database with optional recurring support"""
        # If MongoDB is configured for this process, block writes to the SQLite DB to
        # avoid accidental writes. The bot should be restarted with MONGO_URI set so
        # ReminderStorageMongo is used instead.
        try:
            if os.getenv('MONGO_URI'):
                logger.warning('MONGO_URI present in environment — refusing to write to SQLite reminders.db')
                return -1
        except Exception:
            pass

        # Deduplicate: if an active, unsent reminder with same user/channel/time/message exists,
        # update it with any provided metadata instead of inserting a new row. This prevents
        # duplicate reminders when two code paths race to create the same reminder.
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id FROM reminders
                    WHERE user_id = ? AND channel_id = ? AND reminder_time = ? AND message = ?
                          AND is_active = 1 AND is_sent = 0
                    LIMIT 1
                ''', (user_id, channel_id, reminder_time.isoformat(), message))
                row = cursor.fetchone()
                if row:
                    existing_id = row[0]
                    # Build update for given optional fields
                    updates = {
                        'image_url': image_url,
                        'thumbnail_url': thumbnail_url,
                        'author_name': author_name,
                        'author_icon_url': author_icon_url,
                        'footer_text': footer_text,
                        'footer_icon_url': footer_icon_url,
                        'mention': mention
                    }
                    to_update = {k: v for k, v in updates.items() if v is not None}
                    if to_update:
                        cols = ', '.join([f"{k} = ?" for k in to_update.keys()])
                        vals = list(to_update.values())
                        cursor.execute(f"UPDATE reminders SET {cols} WHERE id = ?", vals + [existing_id])
                        conn.commit()
                        logger.info(f"✅ Updated existing reminder {existing_id} with new metadata instead of inserting duplicate")
                    else:
                        logger.info(f"ℹ️ Found existing identical reminder {existing_id}; not inserting duplicate")
                    return existing_id

                # No duplicate found — insert normally
                cursor.execute('''
                    INSERT INTO reminders (user_id, channel_id, guild_id, message, reminder_time, created_at,
                                         is_recurring, recurrence_type, recurrence_interval, original_time_pattern, mention, image_url,
                                         thumbnail_url, author_name, author_icon_url, footer_text, footer_icon_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id,
                    channel_id,
                    guild_id,
                    message,
                    reminder_time.isoformat(),
                    get_accurate_utc_time().isoformat(),
                    1 if is_recurring else 0,
                    recurrence_type,
                    recurrence_interval,
                    original_pattern,
                    mention,
                    image_url,
                    thumbnail_url,
                    author_name,
                    author_icon_url,
                    footer_text,
                    footer_icon_url
                ))
                reminder_id = cursor.lastrowid
                conn.commit()
                logger.info(f"✅ Added {'recurring ' if is_recurring else ''}reminder {reminder_id} for user {user_id}")
                return reminder_id
        except Exception as e:
            logger.error(f"❌ Failed to add reminder: {e}")
            return -1
    
    def get_due_reminders(self) -> List[Dict]:
        """Get all active reminders that are due"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now = get_accurate_utc_time().isoformat()
                cursor.execute('''
                    SELECT * FROM reminders 
                    WHERE is_active = 1 AND is_sent = 0 AND reminder_time <= ?
                    ORDER BY reminder_time ASC
                ''', (now,))
                
                columns = [desc[0] for desc in cursor.description]
                results = []
                for row in cursor.fetchall():
                    reminder = dict(zip(columns, row))
                    reminder['reminder_time'] = datetime.fromisoformat(reminder['reminder_time'])
                    reminder['created_at'] = datetime.fromisoformat(reminder['created_at'])
                    results.append(reminder)
                
                return results
        except Exception as e:
            logger.error(f"❌ Failed to get due reminders: {e}")
            return []
    
    def mark_reminder_sent(self, reminder_id: int):
        """Mark a reminder as sent"""
        # If running with Mongo configured, avoid updating the SQLite DB
        try:
            if os.getenv('MONGO_URI'):
                logger.warning('MONGO_URI present — skipping SQLite mark_reminder_sent')
                return
        except Exception:
            pass
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE reminders SET is_sent = 1 WHERE id = ? AND is_sent = 0
                ''', (reminder_id,))
                changed = cursor.rowcount
                conn.commit()
                if changed > 0:
                    logger.info(f"✅ Claimed and marked reminder {reminder_id} as sent")
                    return True
                else:
                    logger.debug(f"Reminder {reminder_id} was already claimed/sent")
                    return False
        except Exception as e:
            logger.error(f"❌ Failed to mark reminder as sent: {e}")
            return False

    def update_reminder_fields(self, reminder_id: Union[int, str], fields: dict) -> bool:
        """Update arbitrary fields on a reminder (used to attach images/metadata).

        Only whitelisted columns will be updated to avoid SQL injection.
        """
        if not fields:
            return False
        allowed = {'image_url', 'thumbnail_url', 'author_name', 'author_icon_url', 'footer_text', 'footer_icon_url', 'mention', 'reminder_time'}
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return False
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cols = ', '.join([f"{k} = ?" for k in to_update.keys()])
                vals = list(to_update.values())
                # Support string or int id
                try:
                    rid = int(reminder_id)
                except Exception:
                    rid = reminder_id
                sql = f"UPDATE reminders SET {cols} WHERE id = ?"
                cursor.execute(sql, vals + [rid])
                if cursor.rowcount > 0:
                    conn.commit()
                    logger.info(f"✅ Updated reminder {reminder_id} fields: {list(to_update.keys())}")
                    return True
                return False
        except Exception as e:
            logger.error(f"❌ Failed to update reminder {reminder_id}: {e}")
            return False
    
    def get_user_reminders(self, user_id: str, limit: int = 10) -> List[Dict]:
        """Get active reminders for a specific user"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM reminders 
                    WHERE user_id = ? AND is_active = 1 AND is_sent = 0
                    ORDER BY reminder_time ASC
                    LIMIT ?
                ''', (user_id, limit))
                
                columns = [desc[0] for desc in cursor.description]
                results = []
                for row in cursor.fetchall():
                    reminder = dict(zip(columns, row))
                    reminder['reminder_time'] = datetime.fromisoformat(reminder['reminder_time'])
                    reminder['created_at'] = datetime.fromisoformat(reminder['created_at'])
                    results.append(reminder)
                
                return results
        except Exception as e:
            logger.error(f"❌ Failed to get user reminders: {e}")
            return []
    
    def delete_reminder(self, reminder_id: int, user_id: str) -> bool:
        """Delete a reminder (only if it belongs to the user)"""
        # If Mongo is configured, refuse to delete in SQLite so actions only happen in Mongo
        try:
            if os.getenv('MONGO_URI'):
                logger.warning('MONGO_URI present — skipping SQLite delete_reminder. Use Mongo storage instead.')
                return False
        except Exception:
            pass
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE reminders SET is_active = 0 
                    WHERE id = ? AND user_id = ? AND is_active = 1
                ''', (reminder_id, user_id))
                
                if cursor.rowcount > 0:
                    conn.commit()
                    logger.info(f"✅ Deleted reminder {reminder_id} for user {user_id}")
                    return True
                else:
                    logger.warning(f"❌ No active reminder found with ID {reminder_id} for user {user_id}")
                    return False
        except Exception as e:
            logger.error(f"❌ Failed to delete reminder: {e}")
            return False
    
    def get_all_active_reminders(self) -> List[Dict]:
        """Get ALL active reminders in the system (for admin/mod purposes)"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM reminders 
                    WHERE is_active = 1 AND is_sent = 0
                    ORDER BY reminder_time ASC
                ''')
                
                columns = [desc[0] for desc in cursor.description]
                results = []
                for row in cursor.fetchall():
                    reminder = dict(zip(columns, row))
                    reminder['reminder_time'] = datetime.fromisoformat(reminder['reminder_time'])
                    reminder['created_at'] = datetime.fromisoformat(reminder['created_at'])
                    results.append(reminder)
                
                return results
        except Exception as e:
            logger.error(f"❌ Failed to get all active reminders: {e}")
            return []

class TimeParser:
    """Parses various time formats into datetime objects with timezone support"""
    
    # Timezone mapping for common abbreviations
    TIMEZONE_MAP = {
        'utc': 'UTC',
        'gmt': 'GMT', 
        'est': 'US/Eastern',
        'cst': 'US/Central', 
        'mst': 'US/Mountain',
        'pst': 'US/Pacific',
        'ist': 'Asia/Kolkata',
        'cet': 'Europe/Berlin',  # Changed to Europe/Berlin to properly handle CET/CEST transitions
        'cest': 'Europe/Berlin', # Added explicit CEST support
        'jst': 'Asia/Tokyo',
        'aest': 'Australia/Sydney',
        'bst': 'Europe/London'
    }
    
    @staticmethod
    def extract_timezone(time_str: str) -> tuple[str, Optional[str]]:
        """Extract timezone from time string if present"""
        # Look for timezone at the end of the string
        tz_pattern = r'\b(utc|gmt|est|cst|mst|pst|ist|cet|cest|jst|aest|bst)\b'
        match = re.search(tz_pattern, time_str.lower())
        
        if match:
            tz_abbr = match.group(1)
            # Remove timezone from string
            cleaned_str = re.sub(tz_pattern, '', time_str, flags=re.IGNORECASE).strip()
            return cleaned_str, tz_abbr
        
        return time_str, None
    
    @staticmethod
    def convert_to_timezone(dt: datetime, tz_abbr: str) -> datetime:
        """Convert datetime from specified timezone to UTC for storage"""
        try:
            if tz_abbr.lower() in TimeParser.TIMEZONE_MAP:
                tz_name = TimeParser.TIMEZONE_MAP[tz_abbr.lower()]
                source_tz = pytz.timezone(tz_name)
                utc_tz = pytz.UTC
                
                # If datetime is naive, assume it's in the source timezone
                if dt.tzinfo is None:
                    # Localize to source timezone (this is the user's local time)
                    localized_dt = source_tz.localize(dt)
                    # Convert to UTC for storage
                    utc_dt = localized_dt.astimezone(utc_tz)
                    # Return as naive UTC datetime for database storage
                    return utc_dt.replace(tzinfo=None)
                else:
                    # If already timezone-aware, convert to UTC
                    utc_dt = dt.astimezone(utc_tz)
                    return utc_dt.replace(tzinfo=None)
                
            return dt
        except Exception as e:
            # If timezone conversion fails, return original datetime
            # This ensures we don't break the reminder system
            return dt
    
    @staticmethod
    def get_local_timezone() -> str:
        """Detect system timezone using timedatectl or fallback methods"""
        try:
            import subprocess
            
            # Try to get timezone from timedatectl (most reliable on Linux)
            result = subprocess.run(['timedatectl', 'show', '--property=Timezone'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                tz_line = result.stdout.strip()
                if 'Timezone=' in tz_line:
                    system_tz = tz_line.split('=')[1]
                    
                    # Map system timezone names to our abbreviations
                    timezone_map = {
                        'UTC': 'utc',
                        'GMT': 'utc',
                        'US/Eastern': 'est',
                        'America/New_York': 'est',
                        'US/Central': 'cst', 
                        'America/Chicago': 'cst',
                        'US/Mountain': 'mst',
                        'America/Denver': 'mst',
                        'US/Pacific': 'pst',
                        'America/Los_Angeles': 'pst',
                        'Asia/Kolkata': 'ist',
                        'Asia/Calcutta': 'ist',
                        'CET': 'cet',
                        'CEST': 'cest',
                        'Europe/Berlin': 'cet',
                        'Europe/Paris': 'cet',
                        'Asia/Tokyo': 'jst',
                        'Australia/Sydney': 'aest',
                        'Europe/London': 'bst'
                    }
                    
                    if system_tz in timezone_map:
                        logger.debug(f"Detected system timezone: {system_tz} -> {timezone_map[system_tz]}")
                        return timezone_map[system_tz]
        
        except Exception as e:
            logger.debug(f"Failed to detect timezone via timedatectl: {e}")
        
        # Fallback: Since we set the system to IST, default to IST
        logger.debug("Using fallback timezone: IST")
        return 'ist'
    
    @staticmethod
    def parse_time_string(time_str: str) -> tuple[Optional[datetime], dict]:
        """
        Parse time string into datetime object with timezone support + recurring info
        For relative times ("5 minutes", "1 hour"), uses local timezone
        For absolute times, uses specified timezone or local timezone
        
        Supports formats like:
        RELATIVE TIMES (uses local timezone):
        - "5 minutes", "2 hours", "1 day", "3 weeks"
        - "in 30 minutes", "in 2 hours"
        
        ABSOLUTE TIMES (uses specified or local timezone):
        - "today at 8:50 pm IST", "today at 20:50 UTC"
        - "tomorrow 3pm EST", "tomorrow at 15:30"
        - "2024-12-25 15:30 UTC", "Dec 25 3:30 PM IST"
        
        RECURRING TIMES:
        - "daily at 9am IST", "daily at 21:30"
        - "every 2 days at 8pm", "alternate days at 10am"
        - "weekly at monday 9am", "every week at 15:30"
        
        Returns: (datetime, {"is_recurring": bool, "type": str, "interval": int, "pattern": str})
        """
        original_str = time_str.strip()
        
        # Extract timezone if present
        clean_str, timezone_abbr = TimeParser.extract_timezone(original_str)
        time_str = clean_str.lower()
        
        # Initialize recurring info
        recurring_info = {
            "is_recurring": False,
            "type": None,
            "interval": None,
            "pattern": original_str
        }
        
        # For relative times without explicit timezone, use local timezone
        if not timezone_abbr:
            timezone_abbr = TimeParser.get_local_timezone()
            
        # Get current time in the target timezone for proper comparison
        # If no timezone specified, use UTC
        timezone_for_reference = timezone_abbr if timezone_abbr else 'utc'
        now = get_current_time_in_timezone(timezone_for_reference)
        
        # 1. RECURRING PATTERNS - Check first
        
        # Daily patterns: "daily at 9am", "daily at 21:30"
        daily_match = re.match(r'daily\s+at\s+([0-9]{1,2}):?([0-9]{2})?\s*(am|pm)?', time_str)
        if daily_match:
            hour = int(daily_match.group(1))
            minute = int(daily_match.group(2)) if daily_match.group(2) else 0
            period = daily_match.group(3)
            
            if period == 'pm' and hour != 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
                
            # Set for today at the specified time, or tomorrow if time has passed
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time <= now:
                target_time += timedelta(days=1)
                
            recurring_info.update({
                "is_recurring": True,
                "type": "daily",
                "interval": 1
            })
            
            converted_time = TimeParser.convert_to_timezone(target_time, timezone_abbr)
            return converted_time, recurring_info
        
        # Every N days: "every 2 days at 8pm", "alternate days at 10am" 
        every_days_match = re.match(r'(?:every\s+(\d+)\s+days?|alternate\s+days?)\s+at\s+([0-9]{1,2}):?([0-9]{2})?\s*(am|pm)?', time_str)
        if every_days_match:
            interval = int(every_days_match.group(1)) if every_days_match.group(1) else 2  # "alternate days" = every 2 days
            hour = int(every_days_match.group(2))
            minute = int(every_days_match.group(3)) if every_days_match.group(3) else 0
            period = every_days_match.group(4)
            
            if period == 'pm' and hour != 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
                
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time <= now:
                target_time += timedelta(days=interval)
                
            recurring_info.update({
                "is_recurring": True,
                "type": "days",
                "interval": interval
            })
            
            converted_time = TimeParser.convert_to_timezone(target_time, timezone_abbr)
            return converted_time, recurring_info
        
        # Weekly patterns: "weekly at 15:30", "every week at 9am"
        weekly_match = re.match(r'(?:weekly|every\s+week)\s+at\s+([0-9]{1,2}):?([0-9]{2})?\s*(am|pm)?', time_str)
        if weekly_match:
            hour = int(weekly_match.group(1))
            minute = int(weekly_match.group(2)) if weekly_match.group(2) else 0
            period = weekly_match.group(3)
            
            if period == 'pm' and hour != 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
                
            # Set for same day next week at the specified time
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_time <= now:
                target_time += timedelta(days=7)  # Next week
            else:
                # If time hasn't passed today, schedule for next week anyway (weekly pattern)
                target_time += timedelta(days=7)
                
            recurring_info.update({
                "is_recurring": True,
                "type": "weekly",
                "interval": 7
            })
            
            converted_time = TimeParser.convert_to_timezone(target_time, timezone_abbr)
            return converted_time, recurring_info
        
        # 2. TODAY AT patterns: "today at 8:50 pm", "today at 20:50", "today at 8pm"
        today_match = re.match(r'today\s+at\s+([0-9]{1,2}):?([0-9]{2})?\s*(am|pm)?', time_str)
        if today_match:
            hour = int(today_match.group(1))
            minute = int(today_match.group(2)) if today_match.group(2) else 0
            period = today_match.group(3)
            
            if period == 'pm' and hour != 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
            
            # Create the target time in the specified timezone
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # Get the timezone objects for comparison
            if timezone_abbr.lower() in TimeParser.TIMEZONE_MAP:
                tz_name = TimeParser.TIMEZONE_MAP[timezone_abbr.lower()]
                target_tz = pytz.timezone(tz_name)
                
                # Make times timezone-aware for accurate comparison
                target_time = target_tz.localize(target_time)
                now_aware = target_tz.localize(now)
                
                # Check if this time has already passed TODAY in the target timezone
                if target_time <= now_aware:
                    return None, recurring_info  # Time has passed in target timezone
                
                # Convert to UTC for storage (naive)
                utc_time = target_time.astimezone(pytz.UTC).replace(tzinfo=None)
                return utc_time, recurring_info
            
            # Fallback to simple conversion if timezone is not recognized
            converted_time = TimeParser.convert_to_timezone(target_time, timezone_abbr)
            return converted_time, recurring_info
        
        # 3. RELATIVE TIME patterns: "5 minutes", "2 hours", "in 30 minutes"
        relative_match = re.match(r'(?:in\s+)?(\d+)\s*(minute|min|hour|hr|day|week|month)s?', time_str)
        if relative_match:
            amount = int(relative_match.group(1))
            unit = relative_match.group(2)
            
            # Calculate the future time based on local timezone
            if unit in ['minute', 'min']:
                future_time = now + timedelta(minutes=amount)
            elif unit in ['hour', 'hr']:
                future_time = now + timedelta(hours=amount)
            elif unit == 'day':
                future_time = now + timedelta(days=amount)
            elif unit == 'week':
                future_time = now + timedelta(weeks=amount)
            elif unit == 'month':
                future_time = now + timedelta(days=amount * 30)  # Approximate
            else:
                return None, recurring_info
            
            # Convert from local timezone to UTC for storage
            converted_time = TimeParser.convert_to_timezone(future_time, timezone_abbr)
            return converted_time, recurring_info
        
        # 4. TOMORROW patterns: "tomorrow 3pm", "tomorrow at 15:30"
        if 'tomorrow' in time_str:
            tomorrow = now + timedelta(days=1)
            time_match = re.search(r'(?:at\s+)?([0-9]{1,2}):?([0-9]{2})?\s*(am|pm)?', time_str)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2)) if time_match.group(2) else 0
                period = time_match.group(3)
                
                if period == 'pm' and hour != 12:
                    hour += 12
                elif period == 'am' and hour == 12:
                    hour = 0
                
                result_time = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
                converted_time = TimeParser.convert_to_timezone(result_time, timezone_abbr)
                return converted_time, recurring_info
            else:
                # Default to tomorrow at current time
                result_time = tomorrow
                converted_time = TimeParser.convert_to_timezone(result_time, timezone_abbr)
                return converted_time, recurring_info
        
        # 5. ABSOLUTE DATETIME formats
        try:
            # Try ISO format first
            result_time = datetime.fromisoformat(time_str)
            converted_time = TimeParser.convert_to_timezone(result_time, timezone_abbr)
            return converted_time, recurring_info
        except ValueError:
            pass
        
        # Try common formats
        formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %I:%M %p",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y %I:%M %p",
            "%B %d %H:%M",
            "%B %d %I:%M %p",
            "%b %d %H:%M",
            "%b %d %I:%M %p",
            "%H:%M",
            "%I:%M %p"
        ]
        
        for fmt in formats:
            try:
                parsed_time = datetime.strptime(time_str, fmt)
                # If no year specified, use current year
                if parsed_time.year == 1900:
                    parsed_time = parsed_time.replace(year=now.year)
                # If no date specified, use today
                if parsed_time.date() == datetime(1900, 1, 1).date():
                    parsed_time = now.replace(
                        hour=parsed_time.hour,
                        minute=parsed_time.minute,
                        second=0,
                        microsecond=0
                    )
                    # If the time has passed today, schedule for tomorrow
                    if parsed_time <= now:
                        parsed_time += timedelta(days=1)
                
                # Apply timezone conversion
                converted_time = TimeParser.convert_to_timezone(parsed_time, timezone_abbr)
                return converted_time, recurring_info
            except ValueError:
                continue
        
        return None, recurring_info
    
    @staticmethod
    def utc_to_local(utc_dt: datetime, target_tz_abbr: str = None) -> datetime:
        """Convert UTC datetime back to local timezone for display"""
        try:
            if target_tz_abbr is None:
                target_tz_abbr = TimeParser.get_local_timezone()
                
            if target_tz_abbr.lower() in TimeParser.TIMEZONE_MAP:
                tz_name = TimeParser.TIMEZONE_MAP[target_tz_abbr.lower()]
                target_tz = pytz.timezone(tz_name)
                utc_tz = pytz.UTC
                
                # Assume input is naive UTC datetime
                if utc_dt.tzinfo is None:
                    # Make it timezone-aware as UTC
                    utc_aware = utc_tz.localize(utc_dt)
                    # Convert to target timezone
                    local_dt = utc_aware.astimezone(target_tz)
                    # Return as naive datetime in local timezone
                    return local_dt.replace(tzinfo=None)
                    
            return utc_dt
        except Exception:
            return utc_dt
    
    @staticmethod
    def format_time_until(target_time: datetime) -> str:
        """Format time until target as human-readable string"""
        if target_time is None:
            return "unknown"

        now = get_accurate_utc_time()
        if target_time <= now:
            return "now"

        delta = target_time - now

        if delta.days > 0:
            return f"{delta.days} day{'s' if delta.days != 1 else ''}, {delta.seconds // 3600} hour{'s' if delta.seconds // 3600 != 1 else ''}"
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            minutes = (delta.seconds % 3600) // 60
            return f"{hours} hour{'s' if hours != 1 else ''}, {minutes} minute{'s' if minutes != 1 else ''}"
        else:
            minutes = delta.seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"

class ReminderSystem:
    """Main reminder system that handles commands and background tasks"""
    
    def __init__(self, bot):
        self.bot = bot
        # Prefer MongoDB-backed storage if MONGO_URI is provided; otherwise fall back to SQLite
        try:
            if os.getenv('MONGO_URI'):
                try:
                    from reminder_storage_mongo import ReminderStorageMongo
                    self.storage = ReminderStorageMongo()
                    logger.info('Using MongoDB for reminders storage')
                except Exception as e:
                    logger.exception('Failed to initialize MongoDB storage, falling back to SQLite', exc_info=e)
                    self.storage = ReminderStorage()
            else:
                self.storage = ReminderStorage()
        except Exception:
            # Defensive fallback to ensure bot still runs
            self.storage = ReminderStorage()
        # Don't start task here - let the bot handle it in setup_hook
    
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        self.check_reminders.cancel()
    
    @tasks.loop(minutes=1)
    async def check_reminders(self):
        """Background task to check for due reminders"""
        try:
            due_reminders = self.storage.get_due_reminders()
            
            for reminder in due_reminders:
                try:
                    # Get channel
                    try:
                        channel = self.bot.get_channel(int(reminder['channel_id']))
                    except Exception as e:
                        logger.warning(f"Could not retrieve channel {reminder.get('channel_id')}: {e}")
                        channel = None
                    if not channel:
                        logger.debug(f"Could not find channel {reminder['channel_id']} for reminder {reminder['id']}")
                        continue
                    
                    # Get user
                    user = self.bot.get_user(int(reminder['user_id']))
                    user_mention = f"<@{reminder['user_id']}>" if user else "Unknown User"
                    
                    # Attempt to claim this reminder to avoid duplicates across workers/processes
                    try:
                        claimed = self.storage.mark_reminder_sent(reminder['id'])
                    except Exception:
                        claimed = False
                    if not claimed:
                        logger.debug(f"Skipping reminder {reminder.get('id')} because it was already claimed")
                        continue

                    # Create reminder alert embed with message (no default title)
                    embed = discord.Embed(
                        description=f"{reminder['message']}",
                        color=0xb4a7d6
                    )
                    # Use stored image/thumbnail/author/footer on the reminder if present
                    try:
                        thumb = reminder.get('thumbnail_url')
                        img = reminder.get('image_url')
                        author_n = reminder.get('author_name')
                        author_icon = reminder.get('author_icon_url')
                        footer_t = reminder.get('footer_text')
                        footer_icon = reminder.get('footer_icon_url')

                        if img:
                            embed.set_image(url=img)
                        elif REMINDER_IMAGES.get('alert'):
                            # fallback small thumbnail if no image
                            embed.set_thumbnail(url=REMINDER_IMAGES.get('alert'))

                        if thumb:
                            try:
                                embed.set_thumbnail(url=thumb)
                            except Exception:
                                pass

                        if author_n or author_icon:
                            try:
                                embed.set_author(name=author_n or user_mention, icon_url=author_icon)
                            except Exception:
                                pass

                        if footer_t or footer_icon:
                            try:
                                embed.set_footer(text=footer_t or '', icon_url=footer_icon)
                            except Exception:
                                pass
                    except Exception:
                        # Best-effort: ignore image/author/footer errors and continue
                        pass
                    
                    # Send the reminder with appropriate mention based on stored setting
                    mention_text = ""
                    mention_type = reminder.get('mention', 'everyone')
                    if mention_type == 'everyone':
                        mention_text = "@everyone"
                    elif mention_type == 'user':
                        mention_text = f"<@{reminder['user_id']}>"

                    if channel is not None:
                        try:
                            # Send embed first
                            await channel.send(embed=embed)
                            # Then send the mention as a separate message
                            if mention_text:
                                await channel.send(content=mention_text)
                        except Exception as e:
                            logger.warning(f"Failed to send reminder {reminder['id']} to channel {reminder.get('channel_id')}: {e}")
                            # Don't re-raise; continue to next reminder
                    else:
                        logger.warning(f"Skipping send for reminder {reminder['id']} because channel is missing")
                    
                    # Handle recurring vs one-time reminders
                    if reminder.get('is_recurring', 0):
                        # Reschedule recurring reminder
                        await self._reschedule_recurring_reminder(reminder)
                        logger.info(f"✅ Sent recurring reminder {reminder['id']} and rescheduled next occurrence")
                    else:
                        # One-time reminder was already claimed above; simply log success
                        logger.info(f"✅ Sent one-time reminder {reminder['id']}")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to send reminder {reminder['id']}: {e}")
        
        except Exception as e:
            logger.error(f"❌ Error in check_reminders task: {e}")
    
    async def _reschedule_recurring_reminder(self, reminder: dict):
        """Reschedule a recurring reminder for its next occurrence"""
        try:
            current_time = reminder['reminder_time']
            recurrence_type = reminder.get('recurrence_type')
            interval = reminder.get('recurrence_interval', 1)
            
            # Calculate next occurrence based on recurrence type
            if recurrence_type == 'daily':
                next_time = current_time + timedelta(days=interval)
            elif recurrence_type == 'days':
                next_time = current_time + timedelta(days=interval)
            elif recurrence_type == 'weekly':
                next_time = current_time + timedelta(days=7)
            else:
                # Default to daily if type is unknown
                next_time = current_time + timedelta(days=1)
            # Update the reminder time in storage. If storage provides update_reminder_time use it.
            try:
                if hasattr(self.storage, 'update_reminder_time'):
                    success = self.storage.update_reminder_time(reminder['id'], next_time)
                    if not success:
                        logger.warning(f'Failed to update reminder time for {reminder.get("id")} via storage.update_reminder_time')
                else:
                    # Fallback to SQLite behaviour if original storage is in use
                    with sqlite3.connect(self.storage.db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            UPDATE reminders 
                            SET reminder_time = ?, is_sent = 0
                            WHERE id = ?
                        ''', (next_time.isoformat(), reminder['id']))
                        conn.commit()
            except Exception as e:
                logger.error(f'Failed to reschedule recurring reminder {reminder.get("id")}: {e}')
            logger.info(f"Rescheduled recurring reminder {reminder.get('id')} for {next_time}")
            
        except Exception as e:
            logger.error(f"Failed to reschedule recurring reminder {reminder['id']}: {e}")
    
    @check_reminders.before_loop
    async def before_check_reminders(self):
        """Wait until bot is ready before starting reminder checks"""
        await self.bot.wait_until_ready()
        logger.info("🔄 Reminder checker started")
    
    async def create_reminder(self, interaction: discord.Interaction, time_str: str, message: str, target_channel: discord.TextChannel,
                              mention: str = 'everyone', image_url: str = None, thumbnail_url: str = None,
                              author_name: str = None, author_icon_url: str = None,
                              footer_text: str = None, footer_icon_url: str = None) -> bool:
        """Create a new reminder with timezone and channel support
        
        Note: This method expects the interaction to already be deferred.
        """
        # Channel is now required - no default fallback needed
            
        # Parse the time with timezone support and recurring info
        parsed_result = TimeParser.parse_time_string(time_str)
        reminder_time, recurring_info = parsed_result if parsed_result[0] else (None, {})
        
        # Log timezone information for debugging
        detected_tz = TimeParser.get_local_timezone()
        logger.info(f"Reminder parsing: '{time_str}' | Detected local TZ: {detected_tz.upper()} | Result: {reminder_time} | Recurring: {recurring_info.get('is_recurring', False)}")
        
        if not reminder_time:
            # Get current time in detected timezone for helpful error message
            detected_tz = TimeParser.get_local_timezone()
            current_local = TimeParser.utc_to_local(get_accurate_utc_time(), detected_tz)
            
            try:
                await interaction.followup.send(
                    "❌ **Invalid Time Format or Time Has Passed**\n\n"
                    f"🕰️ **Current time ({detected_tz.upper()}):** {current_local.strftime('%I:%M %p')}\n\n"
                    "I couldn't understand that time format, or the time has already passed today. "
                    "Please try one of these:\n\n"
                    "**SIMPLE TIMES:**\n"
                    "• `5 minutes`, `2 hours`, `1 day`\n"
                    "• `today at 8:50 pm`, `today at 20:30`\n"
                    "• `tomorrow 3pm IST`, `tomorrow at 15:30 UTC`\n\n"
                    "**RECURRING:**\n"
                    "• `daily at 9am IST`, `daily at 21:30`\n"
                    "• `every 2 days at 8pm`, `alternate days at 10am`\n"
                    "• `weekly at 15:30`, `every week at 9am EST`\n\n"
                    "**Supported Timezones:** UTC, GMT, EST, CST, MST, PST, IST, CET, CEST, JST, AEST, BST\n\n"
                    "💡 **Tip:** For 'today at' reminders, make sure the time hasn't passed in the specified timezone!",
                    ephemeral=True
                )
            except Exception as e:
                logger.warning(f"Failed to send invalid-time followup to interaction: {e}")
            return False
        
        # Check if time is in the past
        if reminder_time <= get_accurate_utc_time():
            try:
                await interaction.followup.send(
                    "❌ **Time in the Past**\n\n"
                    "That time has already passed! Please set a reminder for a future time.",
                    ephemeral=True
                )
            except Exception as e:
                logger.warning(f"Failed to send time-in-past followup to interaction: {e}")
            return False
        
        # Add to database with target channel and recurring info
        # If MONGO_URI is set but the process currently has SQLite storage instantiated,
        # try to switch to Mongo at runtime so writes go to Mongo instead of being blocked.
        mongo_init_error = None
        try:
            if os.getenv('MONGO_URI') and self.storage.__class__.__name__ != 'ReminderStorageMongo':
                try:
                    from reminder_storage_mongo import ReminderStorageMongo
                    self.storage = ReminderStorageMongo()
                    logger.info('Switched reminder storage to MongoDB at runtime')
                except Exception as e:
                    mongo_init_error = e
                    logger.exception('Failed to switch to MongoDB at runtime; continuing with current storage', exc_info=e)
        except Exception as e:
            mongo_init_error = e
            logger.exception('Unexpected error while attempting runtime Mongo switch', exc_info=e)

        reminder_id = self.storage.add_reminder(
            user_id=str(interaction.user.id),
            channel_id=str(target_channel.id),
            guild_id=str(interaction.guild.id) if interaction.guild else None,
            message=message,
            reminder_time=reminder_time,
            is_recurring=recurring_info.get('is_recurring', False),
            recurrence_type=recurring_info.get('type'),
            recurrence_interval=recurring_info.get('interval'),
            original_pattern=recurring_info.get('pattern', time_str),
            mention=mention,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            author_name=author_name,
            author_icon_url=author_icon_url,
            footer_text=footer_text,
            footer_icon_url=footer_icon_url
        )
        
        if reminder_id == -1:
            # If Mongo is configured but initialization failed, give a clearer message
            if os.getenv('MONGO_URI'):
                # Prefer the runtime init error if present, otherwise explain generic Mongo configuration issue
                err_msg = None
                if mongo_init_error is not None:
                    err_msg = str(mongo_init_error)
                else:
                    err_msg = 'Unknown error initializing MongoDB client.'

                # Truncate to avoid leaking large traces or secrets
                short_err = (err_msg[:200] + '...') if len(err_msg) > 200 else err_msg
                try:
                    await interaction.followup.send(
                        "❌ **MongoDB Connection Failed**\n\n"
                        "The bot is configured to use MongoDB (MONGO_URI is set) but it failed to initialize. "
                        "Please check your MONGO_URI, ensure `pymongo` is installed, and restart the bot.\n\n"
                        f"Error: {short_err}",
                        ephemeral=True
                    )
                except Exception as e:
                    logger.warning(f"Failed to send Mongo error followup: {e}")
                return False

            # Fallback generic message for SQLite/db errors when Mongo not configured
            await interaction.followup.send(
                "❌ **Database Error**\n\n"
                "Sorry, there was an error saving your reminder. Please try again.",
                ephemeral=True
            )
            return False
        
        # Success response with channel information and recurring info
        time_until = TimeParser.format_time_until(reminder_time)
        
        title = "✅ Reminder Set Successfully!"
        if recurring_info.get('is_recurring'):
            recurrence_type = recurring_info.get('type', 'daily')
            interval = recurring_info.get('interval', 1)
            if recurrence_type == 'daily' and interval == 1:
                title = "✅ Daily Reminder Set!"
            elif recurrence_type == 'days':
                title = f"✅ Recurring Reminder Set (Every {interval} days)!"
            elif recurrence_type == 'weekly':
                title = "✅ Weekly Reminder Set!"
        
        embed = discord.Embed(
            title=title,
            description=f"I'll remind you about: **{message}**",
            color=0x00FF7F
        )
        # Use provided thumbnail if supplied, otherwise fall back to set image
        try:
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            else:
                embed.set_thumbnail(url=image_url or REMINDER_IMAGES.get('set'))
        except Exception:
            pass
        
        # Display scheduled time according to user's timezone preference (if set).
        try:
            user_tz = None
            try:
                # interaction may be provided in the calling context
                user_tz = get_user_timezone(interaction.user.id)
            except Exception:
                user_tz = None

            if user_tz:
                # Convert UTC reminder_time to user's timezone for display
                display_time = TimeParser.utc_to_local(reminder_time, user_tz)
                tz_label = user_tz.upper()
            else:
                # Default to UTC display if user hasn't set a timezone
                display_time = reminder_time
                tz_label = 'UTC'

            embed.add_field(
                name="⏰ Scheduled For",
                value=f"{display_time.strftime('%B %d, %Y at %I:%M %p')} ({tz_label})",
                inline=True
            )
        except Exception:
            # Fallback: show system-local time if something goes wrong
            local_tz = TimeParser.get_local_timezone()
            display_time = TimeParser.utc_to_local(reminder_time, local_tz)
            embed.add_field(
                name="⏰ Scheduled For",
                value=f"{display_time.strftime('%B %d, %Y at %I:%M %p')} ({local_tz.upper()})",
                inline=True
            )
        
        embed.add_field(
            name="⏳ Time Until",
            value=time_until,
            inline=True
        )
        
        embed.add_field(
            name="📍 Reminder ID",
            value=f"#{reminder_id}",
            inline=True
        )
        
        embed.add_field(
            name="📺 Channel",
            value=(target_channel.mention if target_channel else "(unknown channel)"),
            inline=True
        )
        
        # Add recurring information if applicable
        if recurring_info.get('is_recurring'):
            recurrence_type = recurring_info.get('type', 'daily')
            interval = recurring_info.get('interval', 1)
            
            if recurrence_type == 'daily' and interval == 1:
                recurrence_text = "Daily"
            elif recurrence_type == 'days':
                recurrence_text = f"Every {interval} days"
            elif recurrence_type == 'weekly':
                recurrence_text = "Weekly"
            else:
                recurrence_text = "Recurring"
                
            embed.add_field(
                name="🔁 Recurrence",
                value=recurrence_text,
                inline=True
            )
        
        embed.set_footer(text="💡 Use /reminderdashboard to manage your reminders")
        # If the user provided footer text/icon, override footer
        try:
            if footer_text or footer_icon_url:
                embed.set_footer(text=footer_text or "💡 Use /reminderdashboard to manage your reminders", icon_url=footer_icon_url)
        except Exception:
            pass

        # If author fields provided, set author
        try:
            if author_name or author_icon_url:
                embed.set_author(name=author_name or interaction.user.display_name, icon_url=author_icon_url)
        except Exception:
            pass

        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.warning(f"Failed to send reminder success followup to interaction: {e}")

        # Return the reminder id so callers can attach images/modify metadata
        return reminder_id
    
    async def list_user_reminders(self, interaction: discord.Interaction):
        """List all active reminders for the user"""
        user_reminders = self.storage.get_user_reminders(str(interaction.user.id))
        
        if not user_reminders:
            embed = discord.Embed(
                title="📝 Your Reminders",
                description="You don't have any active reminders.\n\nUse `/reminder` to create one!",
                color=0x3498db
            )
            embed.set_thumbnail(url=REMINDER_IMAGES['set'])
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"📝 Your Active Reminders ({len(user_reminders)})",
            color=0x3498db
        )
        embed.set_thumbnail(url=REMINDER_IMAGES['set'])
        
        for i, reminder in enumerate(user_reminders[:10]):  # Limit to 10
            time_until = TimeParser.format_time_until(reminder['reminder_time'])
            
            # Get channel name
            try:
                channel = self.bot.get_channel(int(reminder['channel_id']))
            except Exception as e:
                logger.warning(f"Could not retrieve channel {reminder.get('channel_id')}: {e}")
                channel = None
            channel_info = f"#{channel.name}" if channel else "Unknown Channel"
            
            # Convert UTC time back to local timezone for display
            local_tz = TimeParser.get_local_timezone()
            display_time = TimeParser.utc_to_local(reminder['reminder_time'], local_tz)
            
            embed.add_field(
                name=f"⏰ Reminder #{reminder['id']}",
                value=f"**Message:** {reminder['message'][:80]}{'...' if len(reminder['message']) > 80 else ''}\n"
                      f"**Time:** {display_time.strftime('%B %d, %Y at %I:%M %p')} ({local_tz.upper()})\n"
                      f"**Channel:** {channel_info}\n"
                      f"**In:** {time_until}",
                inline=False
            )
        
        if len(user_reminders) > 10:
            embed.set_footer(text=f"Showing 10 of {len(user_reminders)} reminders. Use /reminderdashboard to manage them.")
        else:
            embed.set_footer(text="💡 Use /reminderdashboard to delete or change timezone for a reminder")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        # Send small previews for reminders that include images (up to 5)
        try:
            preview_count = 0
            for reminder in user_reminders[:10]:
                if preview_count >= 5:
                    break
                img = reminder.get('image_url') or reminder.get('thumbnail_url')
                if not img:
                    continue
                try:
                    preview = discord.Embed(title=f"Preview — Reminder #{reminder.get('id')}", description=(reminder.get('message') or '')[:200], color=0x2f3136)
                    # Prefer full image preview if available
                    if reminder.get('image_url'):
                        preview.set_image(url=reminder.get('image_url'))
                    else:
                        preview.set_thumbnail(url=reminder.get('thumbnail_url'))
                    await interaction.followup.send(embed=preview, ephemeral=True)
                    preview_count += 1
                except Exception:
                    # ignore preview errors and continue
                    continue
        except Exception:
            pass
    
    async def delete_user_reminder(self, interaction: discord.Interaction, reminder_id):
        """Delete a specific reminder.

        reminder_id may be an int (SQLite) or a string (Mongo ObjectId). Attempt to
        coerce to int when possible so underlying SQLite storage still works; otherwise
        pass the raw string through (Mongo storage will accept string/ObjectId).
        """
        # Normalize the id for storage layer: try int conversion first, fall back to raw
        rid = reminder_id
        try:
            rid = int(reminder_id)
        except Exception:
            # keep original (likely Mongo ObjectId string)
            rid = reminder_id

        success = self.storage.delete_reminder(rid, str(interaction.user.id))
        
        if success:
            embed = discord.Embed(
                title="✅ Reminder Deleted",
                description=f"Reminder #{reminder_id} has been successfully removed.",
                color=0x00FF7F
            )
            embed.set_thumbnail(url=REMINDER_IMAGES['set'])
        else:
            embed = discord.Embed(
                title="❌ Reminder Not Found",
                description=f"Could not find an active reminder with ID #{reminder_id} belonging to you.\n\n"
                           "Use `/reminders` to see your active reminders.",
                color=0xFF6B6B
            )
            embed.set_thumbnail(url=REMINDER_IMAGES['set'])
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def list_all_active_reminders(self, interaction: discord.Interaction):
        """List ALL active reminders in the system (admin/mod feature)"""
        try:
            # Get all active reminders
            all_reminders = self.storage.get_all_active_reminders()

            if not all_reminders:
                embed = discord.Embed(
                    title="📝 All Active Reminders",
                    description="There are no active reminders in the system.",
                    color=0x3498db
                )
                embed.set_thumbnail(url=REMINDER_IMAGES['set'])
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            embed = discord.Embed(
                title=f"📝 All Active Reminders ({len(all_reminders)})",
                color=0x3498db
            )
            embed.set_thumbnail(url=REMINDER_IMAGES['set'])

            for i, reminder in enumerate(all_reminders[:15]):  # Limit to 15 for readability
                time_until = TimeParser.format_time_until(reminder['reminder_time'])

                # Get user info
                user = self.bot.get_user(int(reminder['user_id']))
                user_info = user.display_name if user else f"User ID: {reminder['user_id']}"

                # Get channel info
                try:
                    channel = self.bot.get_channel(int(reminder['channel_id']))
                except Exception as e:
                    logger.warning(f"Could not retrieve channel {reminder.get('channel_id')}: {e}")
                    channel = None
                channel_info = f"#{channel.name}" if channel else "Unknown Channel"

                # Convert UTC time back to local timezone for display
                local_tz = TimeParser.get_local_timezone()
                display_time = TimeParser.utc_to_local(reminder['reminder_time'], local_tz)

                # Add recurring indicator
                recurring_indicator = "🔁 " if reminder.get('is_recurring', 0) else ""

                embed.add_field(
                    name=f"⏰ {recurring_indicator}Reminder #{reminder['id']}",
                    value=f"**Message:** {reminder['message'][:60]}{'...' if len(reminder['message']) > 60 else ''}\n"
                          f"**User:** {user_info}\n"
                          f"**Time:** {display_time.strftime('%B %d, %Y at %I:%M %p')} ({local_tz.upper()})\n"
                          f"**Channel:** {channel_info}\n"
                          f"**In:** {time_until}",
                    inline=False
                )

            if len(all_reminders) > 15:
                embed.set_footer(text=f"Showing 15 of {len(all_reminders)} reminders. Use /reminderdashboard to manage them.")
            else:
                embed.set_footer(text="💡 Use /reminderdashboard to delete a reminder")

            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

            # After the main embed, send previews for reminders that include images (up to 8 for admins)
            try:
                preview_count = 0
                for reminder in all_reminders[:15]:
                    if preview_count >= 8:
                        break
                    img = reminder.get('image_url') or reminder.get('thumbnail_url')
                    if not img:
                        continue
                    try:
                        preview = discord.Embed(title=f"Preview — Reminder #{reminder.get('id')}", description=(reminder.get('message') or '')[:200], color=0x2f3136)
                        if reminder.get('image_url'):
                            preview.set_image(url=reminder.get('image_url'))
                        else:
                            preview.set_thumbnail(url=reminder.get('thumbnail_url'))
                        await interaction.followup.send(embed=preview, ephemeral=True)
                        preview_count += 1
                    except Exception:
                        continue
            except Exception:
                pass

        except Exception as e:
            logger.error(f"❌ Failed to list all active reminders: {e}")
            # Check if interaction has already been responded to
            try:
                await interaction.response.send_message(
                    "❌ **Error**\n\n"
                    "An error occurred while fetching all active reminders. Please try again.",
                    ephemeral=True
                )
            except Exception as followup_error:
                logger.warning(f"Could not send error response: {followup_error}")
                # Try to send as followup if response already sent
                try:
                    await interaction.followup.send(
                        "❌ **Error**\n\n"
                        "An error occurred while fetching all active reminders. Please try again.",
                        ephemeral=True
                    )
                except Exception as final_error:
                    logger.error(f"Failed to send error followup: {final_error}")
