"""Tests for the security hardening and reliability fixes."""
import os
import re
import sqlite3
import subprocess
import sys

import pytest

from test_smoke import appmod, client, db, login  # noqa: F401  (shared fixtures/helpers)
from models import MovieRating, Watchlist
from security import CSP, RateLimiter, mysql_engine_options

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------ rate limiter (unit) --
def test_limiter_window_blocking_and_reset():
    now = [1000.0]
    rl = RateLimiter(clock=lambda: now[0])
    assert [rl.allow('k', 3, 60) for _ in range(4)] == [True, True, True, False]
    assert rl.retry_after('k', 60) >= 1
    now[0] += 61
    assert rl.allow('k', 3, 60)                                   # window slid past the old hits
    for _ in range(3):
        rl.record('f', 60)
    assert rl.blocked('f', 3, 60) and not rl.blocked('f', 4, 60)
    rl.reset('f')
    assert not rl.blocked('f', 3, 60)


# ------------------------------------------------------------ throttling (HTTP) --
def fresh_token(c):
    c.get('/login')
    with c.session_transaction() as s:
        return s['_csrf']


def test_login_is_throttled_after_repeated_failures():
    c = appmod.app.test_client()
    appmod.limiter = RateLimiter()
    tok = fresh_token(c)
    codes = [c.post('/login', data={'identifier': 'victim', 'password': 'wrong', 'csrf_token': tok}).status_code for _ in range(10)]
    assert codes[:8] == [200] * 8 and codes[8] == 429 and codes[9] == 429
    assert b'Too many failed attempts' in c.post('/login', data={'identifier': 'victim', 'password': 'x', 'csrf_token': tok}).data
    other = c.post('/login', data={'identifier': 'someone-else', 'password': 'x', 'csrf_token': tok})
    assert other.status_code == 200                               # a different account is not locked out


def test_correct_login_still_works_and_clears_the_counter(client):
    c = appmod.app.test_client()
    tok = fresh_token(c)
    for _ in range(5):
        c.post('/login', data={'identifier': 'a', 'password': 'bad', 'csrf_token': tok})
    assert c.post('/login', data={'identifier': 'a', 'password': 'pw', 'csrf_token': tok}).status_code == 302
    c2 = appmod.app.test_client()
    tok2 = fresh_token(c2)
    for _ in range(5):                                            # counter was reset by the success: 5 more is fine
        assert c2.post('/login', data={'identifier': 'a', 'password': 'bad', 'csrf_token': tok2}).status_code == 200


def test_registration_is_throttled_per_network():
    c = appmod.app.test_client()
    appmod.limiter = RateLimiter()
    tok = fresh_token(c)
    codes = [c.post('/register', data={'username': f'u{i}', 'email': 'bad', 'password': 'x', 'csrf_token': tok}).status_code for i in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_search_is_rate_limited_with_retry_after(client):
    codes = [client.get('/api/search?q=space').status_code for _ in range(92)]
    assert codes[:90] == [200] * 90 and codes[90:] == [429, 429]
    r = client.get('/api/search?q=space')
    assert r.headers['Retry-After'].isdigit() and 'slow down' in r.get_json()['error']


# ------------------------------------------------------------ headers / limits / errors --
def test_security_headers_and_no_inline_scripts(client):
    r = client.get('/')
    h = r.headers
    assert h['Content-Security-Policy'] == CSP and "script-src 'self'" in CSP and 'unsafe-inline' not in CSP.split('style-src')[0]
    assert h['X-Content-Type-Options'] == 'nosniff' and h['X-Frame-Options'] == 'DENY'
    assert h['Referrer-Policy'] and h['Cache-Control'] == 'private, no-store' and 'Cookie' in h['Vary']
    assert 'Strict-Transport-Security' not in h                    # plain http (local) gets no HSTS
    assert 'Strict-Transport-Security' in client.get('/', base_url='https://localhost').headers
    assert 'no-store' not in client.get('/static/style.css').headers.get('Cache-Control', '')
    with appmod.app.app_context():
        u = appmod.User.query.filter_by(username='a').first(); u.is_admin = True; db.session.commit()
    login(client, 'a', 'pw')
    anon = appmod.app.test_client()
    for page in [client.get('/'), client.get('/admin'), client.get('/user/a'), anon.get('/login'), anon.get('/register')]:
        html = page.get_data(as_text=True)
        assert page.status_code == 200
        assert not re.search(r'<script(?![^>]*\bsrc=)', html), 'inline <script> would be blocked by the CSP'
        assert not re.search(r'\son(click|submit|change|load|error)=', html), 'inline event handler would be blocked'


def test_oversized_requests_are_rejected(client):
    r = client.post('/rate_movie', json={'movie_id': 'x', 'rating': 5, 'review': 'a' * 200_000})
    assert r.status_code == 413 and 'too large' in r.get_json()['error']


def test_junk_query_values_do_not_cause_500s(client):
    assert client.get('/api/recommendations?limit=abc').status_code == 200
    assert client.get('/api/recommendations?limit=-5').status_code == 200
    assert client.get('/api/recommendations?limit=99999').status_code == 200
    assert client.get('/api/discover?page=abc').status_code == 200
    assert client.get('/api/discover?page=999999').status_code == 200


def test_errors_are_json_for_api_and_a_page_for_browsers(client, monkeypatch):
    r = client.get('/api/nope')
    assert r.status_code == 404 and r.get_json()['error'] == 'Page not found.'
    page = appmod.app.test_client().get('/definitely-missing')      # a browser navigation: no API headers
    assert page.status_code == 404 and b'Page not found' in page.data and 'text/html' in page.content_type
    monkeypatch.setattr(appmod.recs, 'recommend_for_user', lambda *a, **k: 1 / 0)
    r = client.get('/api/recommendations')
    assert r.status_code == 500 and 'went wrong' in r.get_json()['error']   # JSON, never a raw traceback page


# ------------------------------------------------------------ logout / health / validation --
def test_logout_requires_post_with_csrf(client):
    assert client.get('/logout').status_code == 302 and client.get('/').status_code == 200   # GET does nothing
    c2 = appmod.app.test_client()
    login(c2, 'a', 'pw')
    c2.environ_base.pop('HTTP_X_CSRF_TOKEN')
    assert c2.post('/logout').status_code == 400                    # forged request without a token
    with c2.session_transaction() as s:
        tok = s['_csrf']
    r = c2.post('/logout', data={'csrf_token': tok})
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    assert c2.get('/').status_code == 302                           # really logged out
    assert b'action="/logout" ' in client.get('/').data or b'action="/logout"' in client.get('/').data


def test_healthz_database_check_needs_the_token(client, monkeypatch):
    touched = []
    real = appmod.db.session.execute
    monkeypatch.setattr(appmod.db.session, 'execute', lambda *a, **k: touched.append(1) or real(*a, **k))
    monkeypatch.delenv('HEALTHCHECK_TOKEN', raising=False)
    assert client.get('/healthz?db=1&token=anything').get_json() == {'ok': True} and not touched
    monkeypatch.setenv('HEALTHCHECK_TOKEN', 's3cret')
    client.get('/healthz?db=1&token=wrong'); client.get('/healthz?db=1')
    assert not touched
    assert client.get('/healthz?db=1&token=s3cret').status_code == 200 and touched


def test_registration_rejects_invalid_emails():
    c = appmod.app.test_client()
    appmod.limiter = RateLimiter()
    tok = fresh_token(c)
    for bad in ['', 'plain', 'a@b', '@x.com', 'a b@x.com', 'x' * 130 + '@x.com']:
        r = c.post('/register', data={'username': 'newuser', 'email': bad, 'password': 'longenough', 'csrf_token': tok})
        assert r.status_code == 200 and b'valid email' in r.data, bad
    ok = c.post('/register', data={'username': 'newuser', 'email': 'N@Example.com', 'password': 'longenough', 'csrf_token': tok})
    assert ok.status_code == 302


# ------------------------------------------------------------ data integrity --
def test_database_rejects_duplicate_ratings_and_watchlist_rows(client):
    from sqlalchemy.exc import IntegrityError
    with appmod.app.app_context():
        db.session.add(MovieRating(user_id=1, movie_id='m1', movie_title='t', rating=8)); db.session.commit()
        db.session.add(MovieRating(user_id=1, movie_id='m1', movie_title='t', rating=9))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        db.session.add(Watchlist(user_id=1, movie_id='m1', movie_title='t')); db.session.commit()
        db.session.add(Watchlist(user_id=1, movie_id='m1', movie_title='t'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_rating_and_watchlist_are_idempotent_through_the_api(client):
    for _ in range(3):
        assert client.post('/add_to_watchlist', json={'movie_id': 'tmdb_3'}).status_code == 200
    assert client.post('/rate_movie', json={'movie_id': 'tmdb_1', 'rating': 6}).status_code == 200
    assert client.post('/rate_movie', json={'movie_id': 'tmdb_1', 'rating': 9, 'review': 'better'}).status_code == 200
    with appmod.app.app_context():
        assert Watchlist.query.count() == 1 and MovieRating.query.count() == 1
        assert MovieRating.query.first().rating == 9


# ------------------------------------------------------------ database TLS --
def test_database_tls_is_verified_by_default():
    import certifi
    assert mysql_engine_options({})['connect_args'] == {'ssl': {'ca': certifi.where()}}
    assert mysql_engine_options({'DB_SSL_CA': '/x/ca.pem'})['connect_args'] == {'ssl': {'ca': '/x/ca.pem'}}
    assert mysql_engine_options({'DB_SSL_VERIFY': '0'})['connect_args'] == {'ssl': {'ssl_mode': 'REQUIRED'}}
    assert 'connect_args' not in mysql_engine_options({'DB_SSL': '0'})
    assert mysql_engine_options({})['pool_pre_ping'] is True


# ------------------------------------------------------------ migrations / proxy (fresh processes) --
def run_app_process(db_path, code, **extra_env):
    env = {**os.environ, 'SECRET_KEY': 'x', 'DATABASE_URL': f'sqlite:///{db_path}', **extra_env}
    for k in ('TMDB_API_KEY', 'OMDB_API_KEY', 'RENDER', 'TRUST_PROXY'):
        env.pop(k, None)
    env.update({k: v for k, v in extra_env.items()})
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    return out.stdout


def table_names(path):
    with sqlite3.connect(path) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_is_built_by_migrations(tmp_path):
    path = tmp_path / 'fresh.db'
    run_app_process(path, 'import app')
    names = table_names(path)
    assert {'users', 'movie_ratings', 'watchlist', 'review_likes', 'user_prefs', 'movie_cache',
            'movie_similarity', 'watch_provider_cache', 'alembic_version'} <= names
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT version_num FROM alembic_version').fetchall() == [(appmod.BASELINE_REVISION,)]
    run_app_process(path, 'import app')                              # starting again is a no-op, not an error


def test_database_created_before_migrations_is_adopted(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as c:                                  # what the old create_all() setup produced (no newer tables)
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE, email VARCHAR(120) NOT NULL UNIQUE, password VARCHAR(255) NOT NULL, is_admin BOOLEAN, created_at DATETIME)")
        c.execute("INSERT INTO users VALUES (1,'old','o@x.com','hash',0,'2025-01-01 00:00:00')")
    run_app_process(path, 'import app')
    assert {'user_prefs', 'movie_cache', 'alembic_version'} <= table_names(path)      # missing tables were added
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT username FROM users').fetchall() == [('old',)]       # existing data untouched
        assert c.execute('SELECT version_num FROM alembic_version').fetchall() == [(appmod.BASELINE_REVISION,)]


def test_proxy_headers_are_trusted_only_on_render_or_when_asked(tmp_path):
    code = "import app; print(type(app.app.wsgi_app.application).__name__)"
    assert run_app_process(tmp_path / 'a.db', code).strip() != 'ProxyFix'      # not behind a proxy: do not trust X-Forwarded-*
    assert run_app_process(tmp_path / 'b.db', code, RENDER='true').strip() == 'ProxyFix'
    assert run_app_process(tmp_path / 'c.db', code, TRUST_PROXY='1').strip() == 'ProxyFix'


def test_footer_carries_the_required_credits(client):
    html = client.get('/').get_data(as_text=True)
    assert 'not endorsed or certified by TMDB' in html and 'JustWatch' in html and 'OMDb' in html
