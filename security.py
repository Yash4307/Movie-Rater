"""Security helpers: rate limiting, response headers, database TLS options.

Kept free of Flask globals where possible so each piece can be unit-tested."""
import os
import threading
import time
from collections import defaultdict, deque


# ---------------------------------------------------------------- rate limiting --
class RateLimiter:
    """Sliding-window counters kept in memory (per server process).

    Fine for this deployment (one gunicorn worker). If you ever run several workers or servers, each has
    its own counters, so limits become per-worker; move to Redis (e.g. Flask-Limiter) at that point."""

    def __init__(self, clock=time.time):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()
        self._clock = clock

    def _prune(self, key, window):
        q, cutoff = self._hits[key], self._clock() - window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def count(self, key, window):
        with self._lock:
            return len(self._prune(key, window))

    def retry_after(self, key, window):
        with self._lock:
            q = self._prune(key, window)
            return max(1, int(q[0] + window - self._clock()) + 1) if q else 0

    def record(self, key, window):
        with self._lock:
            self._prune(key, window).append(self._clock())
            if len(self._hits) > 5000:                       # forget idle keys so memory cannot grow forever
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]

    def allow(self, key, limit, window):
        """Counts this call and says whether it is within `limit` calls per `window` seconds."""
        with self._lock:
            q = self._prune(key, window)
            if len(q) >= limit:
                return False
            q.append(self._clock())
            return True

    def blocked(self, key, limit, window):
        return self.count(key, window) >= limit

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


# ---------------------------------------------------------------- response headers --
CSP = '; '.join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com",
    "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com",
    "img-src 'self' data: https:",          # posters come from TMDB (and OMDb as a fallback)
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


def apply_security_headers(response, is_secure, is_static=False):
    h = response.headers
    h.setdefault('Content-Security-Policy', CSP)
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('X-Frame-Options', 'DENY')
    h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    h.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    if is_secure:
        h.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if not is_static:
        # Pages and API answers can be personal, so shared caches must never store them
        # (this also covers the Flask "Vary: Cookie" advisory regardless of proxy behaviour).
        h.setdefault('Cache-Control', 'private, no-store')
        h.add('Vary', 'Cookie')
    return response


# ---------------------------------------------------------------- database TLS --
def mysql_engine_options(env=None):
    """Connection-pool and TLS options for MySQL-family databases (MySQL, TiDB).

    Remote databases are verified against a CA bundle by default (certifi's public CAs, which covers
    TiDB Cloud and other providers using public certificates). DB_SSL_CA points at your own CA file
    (e.g. Aiven's), DB_SSL_VERIFY=0 falls back to encrypted-but-unverified, DB_SSL=0 turns TLS off
    (only for a local database)."""
    env = os.environ if env is None else env
    options = {'pool_pre_ping': True, 'pool_recycle': 280, 'pool_size': 5, 'max_overflow': 2}
    if env.get('DB_SSL', '1') == '0':
        return options
    ca = env.get('DB_SSL_CA')
    if not ca and env.get('DB_SSL_VERIFY', '1') != '0':
        try:
            import certifi
            ca = certifi.where()
        except ImportError:
            ca = None
    options['connect_args'] = {'ssl': {'ca': ca}} if ca else {'ssl': {'ssl_mode': 'REQUIRED'}}
    return options
