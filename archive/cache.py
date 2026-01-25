from datetime import datetime, timedelta

_FILE_CACHE = {}
TTL = timedelta(seconds=30)


def get_cached_file(file_id):
    entry = _FILE_CACHE.get(file_id)
    if not entry:
        return None

    if datetime.utcnow() - entry["ts"] > TTL:
        del _FILE_CACHE[file_id]
        return None

    return entry["data"]


def set_cached_file(file_id, data):
    _FILE_CACHE[file_id] = {
        "data": data,
        "ts": datetime.utcnow()
    }
