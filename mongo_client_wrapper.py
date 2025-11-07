import os
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import ServerSelectionTimeoutError

_DEFAULT_URI = os.getenv('MONGO_URI')


def get_mongo_client(uri: str | None = None, connect_timeout_ms: int = 10000) -> MongoClient:
    """Return a connected MongoClient. Reads MONGO_URI from env if not provided.

    Raises ValueError if no URI is available or RuntimeError if connection fails.
    """
    uri = uri or _DEFAULT_URI
    if not uri:
        raise ValueError('No MongoDB URI provided. Set MONGO_URI in environment or pass uri param')

    client = MongoClient(uri, serverSelectionTimeoutMS=connect_timeout_ms, server_api=ServerApi('1'))
    try:
        client.admin.command('ping')
    except ServerSelectionTimeoutError as e:
        raise RuntimeError(f'Could not connect to MongoDB: {e}') from e
    return client
