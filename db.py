"""
db.py
======
MongoDB Atlas (free M0 tier) connection + per-account search history.

Configure via the MONGODB_URI environment variable (see README.md for the
Atlas free-tier setup steps: create cluster -> database user -> network
access -> connection string). If MONGODB_URI isn't set, is_configured()
returns False and app.py falls back to browser-only (localStorage)
history for that request - the site still works, it just isn't synced
across devices.
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError
from bson import ObjectId
from bson.errors import InvalidId

MONGODB_URI = os.environ.get("MONGODB_URI")
DB_NAME = os.environ.get("MONGODB_DB_NAME", "jobfinder")
HISTORY_LIMIT = 50

_client = None
_index_ready = False


class DBNotConfigured(Exception):
    pass


class DBError(Exception):
    pass


def is_configured():
    return bool(MONGODB_URI)


def _collection():
    global _client, _index_ready
    if not MONGODB_URI:
        raise DBNotConfigured("MONGODB_URI is not set")
    if _client is None:
        _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=6000)
    coll = _client[DB_NAME]["search_history"]
    if not _index_ready:
        # Supports both the history read and the trim-to-50 query without a
        # collection scan as the number of users grows.
        coll.create_index([("email", ASCENDING), ("ts", DESCENDING)])
        _index_ready = True
    return coll


def _serialize(doc):
    return {
        "id": str(doc["_id"]),
        "company": doc.get("company", ""),
        "role": doc.get("role", ""),
        "maxExp": doc.get("maxExp"),
        "days": doc.get("days"),
        "includeSenior": doc.get("includeSenior", False),
        "matchCount": doc.get("matchCount"),
        "platform": doc.get("platform"),
        "ts": doc.get("ts"),
    }


def get_history(email):
    try:
        coll = _collection()
        docs = coll.find({"email": email}).sort("ts", DESCENDING).limit(HISTORY_LIMIT)
        return [_serialize(d) for d in docs]
    except DBNotConfigured:
        raise
    except PyMongoError as e:
        raise DBError(str(e))


def add_history_entry(email, entry):
    try:
        coll = _collection()
        doc = {
            "email": email,
            "company": entry.get("company", ""),
            "role": entry.get("role", ""),
            "maxExp": entry.get("maxExp"),
            "days": entry.get("days"),
            "includeSenior": bool(entry.get("includeSenior", False)),
            "matchCount": entry.get("matchCount"),
            "platform": entry.get("platform"),
            "ts": entry.get("ts") or int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        result = coll.insert_one(doc)
        doc["_id"] = result.inserted_id

        # Keep only the most recent HISTORY_LIMIT entries per user.
        stale_ids = [
            d["_id"] for d in
            coll.find({"email": email}, {"_id": 1}).sort("ts", DESCENDING).skip(HISTORY_LIMIT)
        ]
        if stale_ids:
            coll.delete_many({"_id": {"$in": stale_ids}})

        return _serialize(doc)
    except DBNotConfigured:
        raise
    except PyMongoError as e:
        raise DBError(str(e))


def delete_history_entry(email, entry_id):
    try:
        oid = ObjectId(entry_id)
    except (InvalidId, TypeError):
        raise DBError("invalid entry id")
    try:
        coll = _collection()
        coll.delete_one({"_id": oid, "email": email})
    except DBNotConfigured:
        raise
    except PyMongoError as e:
        raise DBError(str(e))


def clear_history(email):
    try:
        coll = _collection()
        coll.delete_many({"email": email})
    except DBNotConfigured:
        raise
    except PyMongoError as e:
        raise DBError(str(e))
