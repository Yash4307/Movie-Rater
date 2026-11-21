import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_migrate import Migrate
from sqlalchemy import func, or_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from whitenoise import WhiteNoise

load_dotenv()  # must run before the services read their API keys

from models import MovieCache, MovieRating, ReviewLike, User, UserPref, Watchlist, db, utcnow  # noqa: E402
from security import RateLimiter, apply_security_headers, mysql_engine_options  # noqa: E402
from services import movies, providers, recs, sentiment  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('movierater')

app = Flask(__name__)
if os.getenv('RENDER') or os.getenv('TRUST_PROXY') == '1':
    # Behind Render's proxy: trust exactly ONE hop of X-Forwarded-* so remote_addr / is_secure describe the real client.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.wsgi_app = WhiteNoise(app.wsgi_app, root='static/')

secret_key = os.getenv('SECRET_KEY', '')
if not secret_key:
    raise RuntimeError('SECRET_KEY is not set. Add it to .env.')
app.secret_key = secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.getenv('RENDER')),   # Render sets RENDER=true (HTTPS)
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),     # used by "Remember me"
    MAX_CONTENT_LENGTH=64 * 1024,                      # every request here is a tiny form/JSON body
)
APP_NAME = os.getenv('APP_NAME', 'MovieRater')

if not movies.tmdb_enabled() and not os.getenv('OMDB_API_KEY'):
    log.warning('Neither TMDB_API_KEY nor OMDB_API_KEY is set: movie data will come from the cache only.')

# --- Database (MySQL-compatible, e.g. TiDB Cloud, in production) -----------------

def build_database_url():
    raw = os.getenv('DATABASE_URL')
    if not raw:
        user, password, name = os.getenv('DB_USER'), os.getenv('DB_PASSWORD'), os.getenv('DB_NAME')
        if not all([user, password, name]):
            raise RuntimeError('Set DATABASE_URL, or DB_USER / DB_PASSWORD / DB_NAME, in .env')
        raw = (f"mysql+pymysql://{user}:{password}@{os.getenv('DB_HOST', 'localhost')}"
               f"{':' + os.getenv('DB_PORT', '3306')}/{name}")
    url = make_url(raw)
    if url.get_backend_name() == 'mysql':
        # Aiven hands out mysql://...?ssl-mode=REQUIRED; PyMySQL needs its own driver name
        # and chokes on the ssl-mode query arg (we configure SSL via connect_args below).
        url = url.set(drivername='mysql+pymysql', query={})
    return url


db_url = build_database_url()
app.config['SQLALCHEMY_DATABASE_URI'] = db_url.render_as_string(hide_password=False)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if db_url.get_backend_name() == 'mysql':
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = mysql_engine_options()  # TLS is verified by default (see security.py)

db.init_app(app)
migrate = Migrate(app, db)

# --- Rate limiting, errors, headers --------------------------------------------------

limiter = RateLimiter()
EMAIL_RE = re.compile(r'[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}')
LOGIN_WINDOW = 15 * 60


def client_ip():
    return request.remote_addr or 'unknown'


def wants_json():
    return request.path.startswith('/api/') or request.is_json or bool(request.headers.get('X-CSRF-Token'))


def rate_limited(bucket, limit, window=60):
    """Per-user (or per-IP when logged out) request budget; answers 429 with Retry-After when exceeded."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{bucket}:{session.get('user_id') or client_ip()}"
            if not limiter.allow(key, limit, window):
                resp = jsonify({'error': 'Too many requests. Please slow down and try again shortly.'})
                resp.status_code = 429
                resp.headers['Retry-After'] = str(limiter.retry_after(key, window))
                return resp
            return fn(*args, **kwargs)
        return wrapper
    return deco


def int_arg(name, default, lo, hi):
    """Reads an integer query argument; junk or out-of-range values fall back to something safe."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def commit_or_error(message='Could not save your change. Please try again.'):
    """Commits; on failure rolls back and returns a JSON error response (otherwise None)."""
    try:
        db.session.commit()
        return None
    except Exception:
        db.session.rollback()
        log.exception('Commit failed')
        return jsonify({'error': message}), 500


@app.errorhandler(HTTPException)
def http_error(e):
    code = e.code or 500
    message = {400: 'Bad request.', 404: 'Page not found.', 405: 'That action is not allowed here.',
               413: 'That request is too large.', 429: 'Too many requests. Please slow down.',
               500: 'Something went wrong on our side. Please try again.'}.get(code, e.description or 'Something went wrong.')
    if code >= 500:
        db.session.rollback()
    if wants_json():
        return jsonify({'error': message}), code
    return render_template('error.html', code=code, message=message), code


@app.after_request
def add_security_headers(response):
    return apply_security_headers(response, request.is_secure, request.endpoint == 'static')


# --- CSRF + template helpers --------------------------------------------------------

def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


@app.before_request
def load_current_user():
    """Re-checks the logged-in account on every request. Without this, a user an admin just
    deleted (or demoted) kept their old session cookie and could carry on, including writing
    new ratings and keeping admin access, until they logged out themselves."""
    if 'user_id' not in session or request.endpoint in (None, 'static', 'healthz'):
        return None
    user = db.session.get(User, session['user_id'])
    if user is None:
        session.clear()
        flash('This account no longer exists. Please register or log in again.', 'error')
        if request.path.startswith('/api/') or request.is_json or request.headers.get('X-CSRF-Token'):
            return jsonify({'error': 'This account no longer exists.'}), 401
        return redirect(url_for('login'))
    if session.get('username') != user.username:
        session['username'] = user.username
    if session.get('is_admin') != bool(user.is_admin):
        session['is_admin'] = bool(user.is_admin)   # picks up promotions and demotions immediately
    return None


@app.before_request
def csrf_protect():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
        expected = session.get('_csrf', '')
        # Both must be non-empty: compare_digest('', '') is True, which would let a request
        # with no token and no session cookie straight through.
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            if request.path.startswith('/api/') or request.is_json or request.headers.get('X-CSRF-Token') is not None:
                return jsonify({'error': 'Session expired. Please refresh the page.'}), 400
            abort(400)


app.jinja_env.globals['csrf_token'] = csrf_token   # also usable inside imported macros


@app.context_processor
def inject_globals():
    return {'csrf_token': csrf_token, 'app_name': APP_NAME}


@app.template_filter('timeago')
def timeago(dt):
    if not dt:
        return ''
    secs = max(0, int((utcnow() - dt).total_seconds()))
    for unit, size in (('day', 86400), ('hour', 3600), ('min', 60)):
        if secs >= size:
            n = secs // size
            return f'{n} {unit}{"s" if n > 1 and unit != "min" else ""} ago'
    return 'just now'


# --- Auth helpers ---------------------------------------------------------------

def hash_password(password):
    return generate_password_hash(password)


def verify_password(user, password):
    """Accepts new salted hashes AND the old unsalted SHA-256 hashes, upgrading them on login."""
    stored = user.password or ''
    if re.fullmatch(r'[0-9a-f]{64}', stored):
        legacy = hashlib.sha256(password.encode()).hexdigest()
        if hmac.compare_digest(stored, legacy):
            user.password = generate_password_hash(password)
            db.session.commit()
            return True
        return False
    return check_password_hash(stored, password)


def api_login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
        return fn(*args, **kwargs)
    return wrapper


def current_country():
    c = (request.args.get('country') or session.get('country') or 'IN').upper()
    c = c if re.fullmatch(r'[A-Z]{2}', c) else 'IN'
    session['country'] = c
    return c


# --- Pages --------------------------------------------------------------------------

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    uid = session['user_id']

    my_ratings = MovieRating.query.filter_by(user_id=uid).order_by(MovieRating.created_at.desc()).all()
    community_feed = (MovieRating.query.join(User, MovieRating.user_id == User.id)
                     .order_by(MovieRating.created_at.desc()).limit(10).all())
    my_watchlist = Watchlist.query.filter_by(user_id=uid).order_by(Watchlist.added_at.desc()).all()

    top_movies = db.session.query(
        MovieRating.movie_id, MovieRating.movie_title, MovieRating.poster_url,
        func.round(func.avg(MovieRating.rating), 2).label('avg_rating'),
        func.count(MovieRating.id).label('votes')
    ).group_by(MovieRating.movie_id, MovieRating.movie_title, MovieRating.poster_url) \
     .having(func.count(MovieRating.id) >= 1) \
     .order_by(func.avg(MovieRating.rating).desc(), func.count(MovieRating.id).desc()) \
     .limit(10).all()

    reviewed = [r for r in my_ratings if (r.review or '').strip()]
    stats = {'rated': len(my_ratings), 'watchlist': len(my_watchlist), 'reviews': len(reviewed),
             'avg': round(sum(r.rating for r in my_ratings) / len(my_ratings) / 2, 1) if my_ratings else None}

    try:
        recommendations = recs.recommend_for_user(uid, limit=12)
    except Exception:
        log.exception('Recommendations failed')
        recommendations = []
    like_counts, liked_rating_ids = get_feed_like_meta(uid, community_feed)

    return render_template('index.html',
                           recent_ratings=my_ratings,
                           community_feed=community_feed,
                           watchlist=my_watchlist,
                           top_movies=top_movies,
                           stats=stats,
                           now_year=datetime.now().year,
                           genre_names=sorted(g for g in movies.GENRE_IDS if g != 'TV Movie'),
                           recommendations=recommendations,
                           trending_movies=movies.get_trending(10) or [],
                           like_counts=like_counts,
                           liked_rating_ids=liked_rating_ids)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ident = (request.form.get('identifier') or request.form.get('username') or '').strip()
        ip_key, id_key = f'login:ip:{client_ip()}', f'login:id:{ident.lower()[:80]}'
        if limiter.blocked(ip_key, 20, LOGIN_WINDOW) or (ident and limiter.blocked(id_key, 8, LOGIN_WINDOW)):
            wait = max(limiter.retry_after(ip_key, LOGIN_WINDOW), limiter.retry_after(id_key, LOGIN_WINDOW))
            flash(f'Too many failed attempts. Please try again in about {wait // 60 + 1} minutes.', 'error')
            return render_template('login.html'), 429
        user = User.query.filter(or_(func.lower(User.email) == ident.lower(), User.username == ident)).first() \
            if ident else None
        if user and verify_password(user, request.form.get('password', '')):
            limiter.reset(id_key)
            session.clear()  # fresh session on login (prevents session fixation)
            session.permanent = bool(request.form.get('remember'))
            session['user_id'] = user.id
            session['username'] = user.username
            session['is_admin'] = user.is_admin
            pref = db.session.get(UserPref, user.id)  # theme belongs to the account, not the browser
            session['theme'] = pref.theme if pref and pref.theme in ('dark', 'light') else 'dark'
            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        limiter.record(ip_key, LOGIN_WINDOW)
        limiter.record(id_key, LOGIN_WINDOW)
        flash('Invalid email/username or password', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if not limiter.allow(f'register:{client_ip()}', 10, 3600):
            flash('Too many sign-up attempts from this network. Please try again later.', 'error')
            return render_template('register.html'), 429
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,30}', username):
            flash('Username must be 3-30 characters: letters, numbers, . _ -', 'error')
            return render_template('register.html')
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('register.html')
        if len(email) > 120 or not EMAIL_RE.fullmatch(email):
            flash('Please enter a valid email address.', 'error')
            return render_template('register.html')
        try:
            db.session.add(User(username=username, email=email, password=hash_password(password)))
            db.session.commit()
            flash('Registration successful!', 'success')
            return redirect(url_for('login'))
        except IntegrityError:
            db.session.rollback()
            flash('Username or email already exists', 'error')
    return render_template('register.html')


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    if request.method == 'POST':          # POST + CSRF token, so another site cannot log you out with a link or image
        session.clear()
        flash('Logged out', 'info')
        return redirect(url_for('login'))
    return redirect(url_for('index'))


@app.route('/healthz')
def healthz():
    """Point an uptime pinger here. ?db=1&token=... also touches the database (keeps it awake), but only when
    HEALTHCHECK_TOKEN is set and matches, so strangers cannot use it to generate database load."""
    token = os.getenv('HEALTHCHECK_TOKEN', '')
    if request.args.get('db') and token and hmac.compare_digest(request.args.get('token', ''), token):
        db.session.execute(db.text('SELECT 1'))
    return jsonify({'ok': True})


# --- Legacy endpoints (same JSON shape the current front-end expects) -----------------

@app.route('/search_movie', methods=['POST'])
@api_login_required
@rate_limited('search', 90)
def search_movie():
    body = request.get_json(silent=True) or {}
    ref = (body.get('ref') or '').strip()
    title = (body.get('title') or '').strip()
    if not ref and not title:
        return jsonify({'error': 'Title required'}), 400
    try:
        if not ref:
            hits = movies.search_movies(title)
            ref = hits[0]['ref'] if hits else ''
        movie = movies.get_movie(ref) if ref else None
        return jsonify(movie) if movie else (jsonify({'error': 'Not found'}), 404)
    except Exception:
        log.exception('search_movie failed')
        return jsonify({'error': 'Search failed, please try again'}), 500


@app.route('/search_suggestions')
@api_login_required
@rate_limited('search', 90)
def search_suggestions():
    return jsonify(movies.search_movies(request.args.get('q', ''), limit=6))


@app.route('/rate_movie', methods=['POST'])
@api_login_required
def rate_movie():
    data = request.get_json(silent=True) or {}
    movie_id = str(data.get('movie_id') or '').strip()
    try:
        rating = int(data.get('rating'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Rating must be a number'}), 400
    if not movie_id or not 1 <= rating <= 10:
        return jsonify({'error': 'Invalid movie or rating'}), 400

    meta = movies.get_movie(movie_id, enrich=False) or {}  # server data wins over client-sent data
    title = meta.get('title') or data.get('movie_title') or 'Untitled'
    poster = meta.get('poster') or data.get('poster_url')
    genres = ','.join(meta.get('genres') or data.get('genres') or [])
    movie_id = meta.get('id', movie_id)
    review = (data.get('review') or '')[:5000]

    for attempt in (1, 2):
        try:
            existing = MovieRating.query.filter_by(user_id=session['user_id'], movie_id=movie_id).first()
            if existing:
                existing.rating, existing.review = rating, review
                existing.poster_url, existing.genres = poster, genres
            else:
                db.session.add(MovieRating(user_id=session['user_id'], movie_id=movie_id, movie_title=title,
                                           poster_url=poster, rating=rating, review=review, genres=genres))
            Watchlist.query.filter_by(user_id=session['user_id'], movie_id=movie_id).delete()
            db.session.commit()
            return jsonify({'success': 'Saved!'})
        except IntegrityError:
            db.session.rollback()          # another request saved this movie first: retry as an update
            if attempt == 2:
                break
        except Exception:
            db.session.rollback()
            log.exception('rate_movie failed')
            break
    return jsonify({'error': 'Could not save rating'}), 500


@app.route('/add_to_watchlist', methods=['POST'])
@api_login_required
def add_to_watchlist():
    data = request.get_json(silent=True) or {}
    movie_id = str(data.get('movie_id') or '').strip()
    if not movie_id:
        return jsonify({'error': 'movie_id required'}), 400
    meta = movies.get_movie(movie_id, enrich=False) or {}
    movie_id = meta.get('id', movie_id)
    if not Watchlist.query.filter_by(user_id=session['user_id'], movie_id=movie_id).first():
        db.session.add(Watchlist(user_id=session['user_id'], movie_id=movie_id,
                                 movie_title=(meta.get('title') or data.get('movie_title') or 'Untitled')[:255],
                                 poster_url=meta.get('poster') or data.get('poster_url')))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()          # added twice at the same moment: it is on the list, which is the goal
        except Exception:
            db.session.rollback()
            log.exception('add_to_watchlist failed')
            return jsonify({'error': 'Could not update your watchlist.'}), 500
    return jsonify({'success': 'Added to watchlist'})


@app.route('/remove_from_watchlist', methods=['POST'])
@api_login_required
def remove_from_watchlist():
    movie_id = str((request.get_json(silent=True) or {}).get('movie_id') or '')
    Watchlist.query.filter_by(user_id=session['user_id'], movie_id=movie_id).delete()
    failed = commit_or_error('Could not update your watchlist.')
    return failed or jsonify({'success': 'Removed'})


@app.route('/like_review/<int:rating_id>', methods=['POST'])
@api_login_required
def like_review(rating_id):
    rating = db.session.get(MovieRating, rating_id)
    if not rating:
        return jsonify({'error': 'Review not found'}), 404
    if rating.user_id == session['user_id']:
        return jsonify({'error': 'Cannot like your own review'}), 400
    existing = ReviewLike.query.filter_by(user_id=session['user_id'], rating_id=rating_id).first()
    if existing:
        db.session.delete(existing)
        liked = False
    else:
        db.session.add(ReviewLike(user_id=session['user_id'], rating_id=rating_id))
        liked = True
    try:
        db.session.commit()
    except IntegrityError:                 # a double-click raced itself; report the state that won
        db.session.rollback()
        liked = ReviewLike.query.filter_by(user_id=session['user_id'], rating_id=rating_id).first() is not None
    except Exception:
        db.session.rollback()
        log.exception('like_review failed')
        return jsonify({'error': 'Could not update your like.'}), 500
    return jsonify({'success': True, 'liked': liked,
                    'count': ReviewLike.query.filter_by(rating_id=rating_id).count()})


@app.route('/user/<username>')
def user_profile(username):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.filter_by(username=username).first_or_404()
    ratings = MovieRating.query.filter_by(user_id=user.id).order_by(MovieRating.created_at.desc()).all()
    watchlist = Watchlist.query.filter_by(user_id=user.id).order_by(Watchlist.added_at.desc()).all()
    return render_template('profile.html', profile_user=user, ratings=ratings, watchlist=watchlist,
                           taste=recs.taste_profile(user.id))


# --- New JSON API (for the dynamic UI) ---------------------------------------------------

@app.route('/api/search')
@api_login_required
@rate_limited('search', 90)
def api_search():
    return jsonify(movies.search_movies(request.args.get('q', ''), limit=8))


@app.route('/api/movie/<ref>')
@api_login_required
@rate_limited('movie', 90)
def api_movie(ref):
    movie = movies.get_movie(ref)
    if not movie:
        return jsonify({'error': 'Not found'}), 404
    uid = session['user_id']
    mine = MovieRating.query.filter_by(user_id=uid, movie_id=movie['id']).first()
    stats = db.session.query(func.avg(MovieRating.rating), func.count(MovieRating.id)) \
        .filter(MovieRating.movie_id == movie['id']).one()
    movie['community'] = {'average_stars': round(float(stats[0]) / 2, 2) if stats[0] else None,
                          'votes': stats[1]}
    movie['my_rating'] = {'stars': mine.rating / 2, 'review': mine.review} if mine else None
    movie['in_watchlist'] = bool(Watchlist.query.filter_by(user_id=uid, movie_id=movie['id']).first())
    return jsonify(movie)


@app.route('/api/movie/<ref>/where-to-watch')
@api_login_required
@rate_limited('movie', 90)
def api_where_to_watch(ref):
    result = providers.get_watch_options(ref, current_country())
    return jsonify(result) if result else (jsonify({'error': 'Not found'}), 404)


@app.route('/api/movie/<ref>/similar')
@api_login_required
@rate_limited('movie', 90)
def api_similar(ref):
    movie = movies.get_movie(ref, enrich=False)
    return jsonify(recs.similar_to(movie['id']) if movie else [])


@app.route('/api/movie/<ref>/reviews')
@api_login_required
def api_reviews(ref):
    movie = movies.get_movie(ref, enrich=False)
    if not movie:
        return jsonify({'error': 'Not found'}), 404
    rows = (MovieRating.query.join(User, MovieRating.user_id == User.id)
            .filter(MovieRating.movie_id == movie['id'], MovieRating.review != '')
            .order_by(MovieRating.created_at.desc()).limit(30).all())
    like_counts, liked_ids = get_feed_like_meta(session['user_id'], rows)
    reviews = []
    for r in rows:
        s = sentiment.analyze(r.review)
        reviews.append({'id': r.id, 'user': r.user.username, 'stars': r.rating / 2, 'review': r.review,
                        'sentiment': s['label'] if s else None, 'likes': like_counts.get(r.id, 0),
                        'liked_by_me': r.id in liked_ids,
                        'created_at': r.created_at.isoformat() if r.created_at else None})
    return jsonify({'sentiment': sentiment.summarize([r.review for r in rows]), 'reviews': reviews})


@app.route('/api/recommendations')
@api_login_required
def api_recommendations():
    return jsonify(recs.recommend_for_user(session['user_id'], limit=int_arg('limit', 12, 1, 24)))


@app.route('/api/taste')
@api_login_required
def api_taste():
    return jsonify(recs.taste_profile(session['user_id']))


@app.route('/api/trending')
@api_login_required
def api_trending():
    return jsonify(movies.get_trending(12) or [])


@app.route('/api/discover')
@api_login_required
@rate_limited('discover', 60)
def api_discover():
    a = request.args
    return jsonify(movies.discover(genre=a.get('genre'), sort=a.get('sort', 'popular'), year=a.get('year'),
                                   provider=a.get('provider'), free=a.get('free') == '1',
                                   country=current_country(), page=int_arg('page', 1, 1, 50)))


@app.route('/api/theme', methods=['POST'])
@api_login_required
def api_theme():
    theme = (request.get_json(silent=True) or {}).get('theme')
    if theme not in ('dark', 'light'):
        return jsonify({'error': 'Invalid theme'}), 400
    pref = db.session.get(UserPref, session['user_id'])
    if pref:
        pref.theme = theme
    else:
        db.session.add(UserPref(user_id=session['user_id'], theme=theme))
    failed = commit_or_error('Could not save your theme.')
    if failed:
        return failed
    session['theme'] = theme
    return jsonify({'theme': theme})


@app.route('/api/countries')
@api_login_required
def api_countries():
    return jsonify(movies.region_list() or [])


@app.route('/api/providers')
@api_login_required
def api_providers():
    return jsonify(movies.provider_list(current_country()) or [])


@app.route('/api/watchlist/where-to-watch')
@api_login_required
@rate_limited('optimizer', 6)
def api_watchlist_where_to_watch():
    return jsonify(providers.optimize_watchlist(session['user_id'], current_country()))


# --- Admin ----------------------------------------------------------------------------------

@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    users = User.query.order_by(User.created_at.desc()).all()
    avg = db.session.query(func.avg(MovieRating.rating)).scalar()
    dist = {s: 0 for s in range(1, 6)}
    for rating, n in db.session.query(MovieRating.rating, func.count(MovieRating.id)).group_by(MovieRating.rating):
        dist[min(5, max(1, -(-rating // 2)))] += n
    total_ratings = sum(dist.values())
    activity = [(r.created_at, 'rating', r) for r in
                MovieRating.query.join(User, MovieRating.user_id == User.id)
                .order_by(MovieRating.created_at.desc()).limit(8).all()]
    activity += [(u.created_at, 'user', u) for u in users[:5]]
    activity.sort(key=lambda x: x[0] or datetime.min, reverse=True)
    return render_template(
        'admin.html', users=users, activity=activity[:8],
        stats={'movies': MovieCache.query.count(), 'users': len(users),
               'reviews': MovieRating.query.filter(MovieRating.review.isnot(None), MovieRating.review != '').count(),
               'avg': round(float(avg) / 2, 1) if avg else None, 'ratings': total_ratings},
        distribution=[{'stars': s, 'count': dist[s],
                       'pct': round(dist[s] * 100 / total_ratings) if total_ratings else 0} for s in range(5, 0, -1)])


@app.route('/admin/toggle_admin/<int:user_id>', methods=['POST'])
def toggle_admin(user_id):
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    if user_id == session.get('user_id'):
        flash('You cannot change your own admin role.', 'error')
        return redirect(url_for('admin_panel') + '#users')
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_panel') + '#users')
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f'Updated admin role for {user.username}.', 'success')
    return redirect(url_for('admin_panel') + '#users')


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if not session.get('is_admin') or user_id == session['user_id']:
        return redirect(url_for('admin_panel'))
    user = db.session.get(User, user_id)
    if user:
        purge_user(user)
        flash('User deleted.', 'success')
    return redirect(url_for('admin_panel') + '#users')


def purge_user(user):
    """Deletes a user and everything that points at them. Likes reference ratings, so they go first."""
    uid = user.id
    own_rating_ids = [r.id for r in MovieRating.query.filter_by(user_id=uid).all()]
    if own_rating_ids:
        ReviewLike.query.filter(ReviewLike.rating_id.in_(own_rating_ids)).delete(synchronize_session=False)
    ReviewLike.query.filter_by(user_id=uid).delete()
    MovieRating.query.filter_by(user_id=uid).delete()
    Watchlist.query.filter_by(user_id=uid).delete()
    UserPref.query.filter_by(user_id=uid).delete()
    db.session.delete(user)
    db.session.commit()


@app.route('/account/delete', methods=['POST'])
def delete_account():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    back = redirect(url_for('user_profile', username=user.username))
    if request.form.get('confirm_username', '') != user.username:
        flash('That username did not match, so your account was not deleted.', 'error')
        return back
    if user.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash('You are the only admin. Make another user an admin before deleting this account.', 'error')
        return back
    try:
        purge_user(user)
    except Exception:
        db.session.rollback()
        log.exception('Account deletion failed')
        flash('Could not delete your account. Please try again.', 'error')
        return back
    session.clear()
    flash('Your account and all of its data were permanently deleted.', 'success')
    return redirect(url_for('login'))


def get_feed_like_meta(current_user_id, feed):
    rating_ids = [r.id for r in feed]
    if not rating_ids:
        return {}, set()
    rows = db.session.query(ReviewLike.rating_id, func.count(ReviewLike.id)) \
        .filter(ReviewLike.rating_id.in_(rating_ids)).group_by(ReviewLike.rating_id).all()
    liked = ReviewLike.query.filter(ReviewLike.user_id == current_user_id,
                                    ReviewLike.rating_id.in_(rating_ids)).all()
    return {rid: n for rid, n in rows}, {r.rating_id for r in liked}


MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')
BASELINE_REVISION = '4d343552c7bd'


def init_schema():
    """Brings the database up to date with the Alembic migrations in migrations/ (run automatically on start,
    so Render's free plan, which has no shell, still gets schema changes).

    * fresh database          -> migrations create every table
    * created before migrations existed (db.create_all()) -> missing tables are added, then it is stamped at the
                                 baseline revision so later migrations apply normally
    Set SKIP_SCHEMA_INIT=1 to skip this (used while generating a new migration)."""
    if os.getenv('SKIP_SCHEMA_INIT') == '1':
        return
    if not os.path.isdir(MIGRATIONS_DIR):
        db.create_all()
        return
    from flask_migrate import stamp, upgrade
    tables = set(sa_inspect(db.engine).get_table_names())
    if tables and 'alembic_version' not in tables:
        db.create_all()
        stamp(directory=MIGRATIONS_DIR, revision=BASELINE_REVISION)
    upgrade(directory=MIGRATIONS_DIR)


with app.app_context():
    init_schema()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
