"""Tiny in-process TTL cache (per worker). Falsy results are never cached, so a
failed upstream call is retried on the next request instead of being remembered."""
import threading
import time
from functools import wraps

_lock = threading.Lock()


def ttl_cache(seconds, maxsize=512):
    def decorator(fn):
        store = {}

        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            with _lock:
                hit = store.get(key)
                if hit and hit[0] > now:
                    return hit[1]
            value = fn(*args, **kwargs)
            if value:
                with _lock:
                    if len(store) >= maxsize:
                        store.pop(next(iter(store)))
                    store[key] = (now + seconds, value)
            return value

        wrapper.cache_clear = store.clear
        return wrapper
    return decorator
