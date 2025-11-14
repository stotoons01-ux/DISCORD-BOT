import os
import sys
import logging
from datetime import datetime
from typing import Dict, Any, Optional

# Ensure the project root (the directory that contains the `db` package)
# is on sys.path so imports like `db.mongo_adapters` work regardless of
# the current working directory (some hosts run with a different cwd).
proj_root = os.path.dirname(__file__)
if proj_root not in sys.path:
    sys.path.insert(0, proj_root)

# Try to import the real mongo adapters from the packaged `db` package.
# If that fails (running from a different working dir), provide safe
# fallbacks so the application continues to work using SQLite/local files.
try:
    from db.mongo_adapters import *  # type: ignore
    # Re-exported names will come from the real module
    __all__ = [
        'mongo_enabled', 'UserTimezonesAdapter', 'BirthdaysAdapter', 'UserProfilesAdapter', 'GiftcodeStateAdapter', 'GiftCodesAdapter'
    ]
except Exception:
    logging.getLogger(__name__).warning('db.mongo_adapters import failed; using local fallback shim')

    def mongo_enabled() -> bool:
        return False

    class _FallbackAdapter:
        @staticmethod
        def load_all():
            return {}

        @staticmethod
        def get(*args, **kwargs):
            return None

        @staticmethod
        def set(*args, **kwargs):
            return False

        @staticmethod
        def remove(*args, **kwargs):
            return False

        @staticmethod
        def clear_all(*args, **kwargs):
            return False

    # Provide minimal fallback classes expected by the codebase
    class UserTimezonesAdapter(_FallbackAdapter):
        pass

    class BirthdaysAdapter(_FallbackAdapter):
        pass

    class UserProfilesAdapter(_FallbackAdapter):
        @staticmethod
        def load_all() -> Dict[str, Any]:
            return {}

        @staticmethod
        def get(user_id: str) -> Optional[Dict[str, Any]]:
            return None

        @staticmethod
        def set(user_id: str, data: Dict[str, Any]) -> bool:
            return False

    class GiftcodeStateAdapter(_FallbackAdapter):
        @staticmethod
        def get_state() -> Dict[str, Any]:
            return {}

        @staticmethod
        def set_state(state: Dict[str, Any]) -> bool:
            return False

    class GiftCodesAdapter(_FallbackAdapter):
        @staticmethod
        def get_all():
            return []

        @staticmethod
        def insert(code: str, date: str, validation_status: str = 'pending') -> bool:
            return False

        @staticmethod
        def update_status(code: str, validation_status: str) -> bool:
            return False

        @staticmethod
        def delete(code: str) -> bool:
            return False

    __all__ = [
        'mongo_enabled', 'UserTimezonesAdapter', 'BirthdaysAdapter', 'UserProfilesAdapter', 'GiftcodeStateAdapter', 'GiftCodesAdapter'
    ]

