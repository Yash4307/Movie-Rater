"""Movie data layer.

TMDB is the primary source (posters, overview, cast, genres, trailer, imdb_id).
OMDb is only used to add the IMDb rating / Rotten Tomatoes score, and as a
fallback when TMDB is unavailable. Everything is cached in MySQL (movie_cache),
so a movie costs at most one TMDB call + one OMDb call per week.
"""
import json
import logging
import os
import time
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from sqlalchemy.exc import IntegrityError
from urllib3.util.retry import Retry

from models import MovieCache, db, utcnow
from services.cache import ttl_cache

log = logging.getLogger(__name__)

TMDB_URL = 'https://api.themoviedb.org/3'
OMDB_URL = 'https://www.omdbapi.com/'
IMG_URL = 'https://image.tmdb.org/t/p'
PLACEHOLDER_POSTER = '/static/img/no-poster.svg'
CACHE_TTL = timedelta(days=7)

# Keep genre names consistent with what Cinemagoer stored in older ratings.
GENRE_ALIASES = {'Science Fiction': 'Sci-Fi'}
TMDB_GENRES = {
    28: 'Action', 12: 'Adventure', 16: 'Animation', 35: 'Comedy', 80: 'Crime',
    99: 'Documentary', 18: 'Drama', 10751: 'Family', 14: 'Fantasy', 36: 'History',
    27: 'Horror', 10402: 'Music', 9648: 'Mystery', 10749: 'Romance',
    878: 'Sci-Fi', 10770: 'TV Movie', 53: 'Thriller', 10752: 'War', 37: 'Western',
}

_http = requests.Session()
_http.mount('https://', HTTPAdapter(
    max_retries=Retry(total=2, backoff_factor=0.4,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=('GET',)),
    pool_maxsize=10))

_omdb_blocked_until = 0.0


def _tmdb_key():
    return os.getenv('TMDB_API_KEY', '').strip()


def _omdb_key():
    return os.getenv('OMDB_API_KEY', '').strip()


def tmdb_enabled():
    return bool(_tmdb_key())


# --------------------------------------------------------------------------
# Low-level HTTP. Never log the exception text: requests puts the full URL
# (including the API key) in it.
# --------------------------------------------------------------------------

def _tmdb(path, **params):
    key = _tmdb_key()
    if not key:
        return None
    headers = {}
    if key.startswith('eyJ'):  # v4 "read access token"
        headers['Authorization'] = f'Bearer {key}'
    else:
        params['api_key'] = key
    try:
        r = _http.get(f'{TMDB_URL}{path}', params=params, headers=headers, timeout=(3, 6))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning('TMDB request %s failed (%s)', path, type(exc).__name__)
        return None


def _omdb(**params):
    global _omdb_blocked_until
    key = _omdb_key()
    if not key or time.time() < _omdb_blocked_until:
        return None
    try:
        r = _http.get(OMDB_URL, params={'apikey': key, **params}, timeout=(3, 6))
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning('OMDb request failed (%s)', type(exc).__name__)
        return None
    if data.get('Response') != 'True':
        if 'limit' in str(data.get('Error', '')).lower():
            _omdb_blocked_until = time.time() + 3600
            log.warning('OMDb daily limit reached; pausing OMDb calls for 1 hour')
        return None
    return data


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def img(path, size='w342'):
    if not path:
        return ''
    return path if path.startswith('http') else f'{IMG_URL}/{size}{path}'


def _year(date_str):
    return date_str[:4] if date_str and len(date_str) >= 4 else ''


def _imdb_digits(imdb_id):
    """'tt0133093' -> '0133093' (the id format Cinemagoer used, so old ratings still match)."""
    if imdb_id and imdb_id.startswith('tt') and imdb_id[2:].isdigit():
        return imdb_id[2:]
    return None


def _fmt_rating(value):
    return f'{value:.1f}' if isinstance(value, (int, float)) and value else 'N/A'


def movie_to_dict(row):
    genres = [g for g in (row.genres or '').split(',') if g]
    try:
        cast = json.loads(row.cast_json) if row.cast_json else []
    except ValueError:
        cast = []
    for c in cast:
        c['photo'] = img(c.get('photo'), 'w185')
    main_rating = row.imdb_rating or row.tmdb_rating
    return {
        'id': row.movie_id,
        'tmdb_id': row.tmdb_id,
        'imdb_id': row.imdb_id,
        'title': row.title,
        'year': row.year or 'N/A',
        'poster': img(row.poster_path, 'w500') or PLACEHOLDER_POSTER,
        'backdrop': img(row.backdrop_path, 'w1280'),
        'plot': row.overview or 'No summary available.',
        'tagline': row.tagline or '',
        'genres': genres,
        'runtime': row.runtime,
        'director': row.director or '',
        'cast': cast,
        'language': row.original_language,
        'rating': _fmt_rating(main_rating),
        'imdb_rating': _fmt_rating(row.imdb_rating) if row.imdb_rating else None,
        'rt_score': row.rt_score,
        'tmdb_rating': _fmt_rating(row.tmdb_rating) if row.tmdb_rating else None,
        'trailer_url': f'https://www.youtube.com/watch?v={row.trailer_key}' if row.trailer_key else None,
    }


def movie_card(row):
    """Light shape for grids/carousels."""
    return {
        'id': row.movie_id,
        'title': row.title,
        'year': row.year or 'N/A',
        'poster': img(row.poster_path, 'w342') or PLACEHOLDER_POSTER,
        'backdrop': img(row.backdrop_path, 'w1280'),
        'overview': row.overview or '',
        'rating': _fmt_rating(row.imdb_rating or row.tmdb_rating),
        'genres': [g for g in (row.genres or '').split(',') if g],
    }


def _tmdb_card(m):
    return {
        'id': f'tmdb_{m["id"]}',
        'title': m.get('title') or m.get('original_title') or 'Untitled',
        'year': _year(m.get('release_date')) or 'N/A',
        'poster': img(m.get('poster_path'), 'w342') or PLACEHOLDER_POSTER,
        'backdrop': img(m.get('backdrop_path'), 'w1280'),
        'overview': m.get('overview') or '',
        'rating': _fmt_rating(m.get('vote_average')),
        'genres': [TMDB_GENRES[g] for g in m.get('genre_ids', []) if g in TMDB_GENRES],
    }


# --------------------------------------------------------------------------
# Cache read / write
# --------------------------------------------------------------------------

def _cache_lookup(ref):
    if ref.startswith('tmdb_'):
        try:
            return MovieCache.query.filter_by(tmdb_id=int(ref[5:])).first()
        except ValueError:
            return None
    if ref.startswith('tt') and ref[2:].isdigit():
        ref = ref[2:]
    if ref.isdigit():
        return db.session.get(MovieCache, ref)
    return None


def _needs_omdb(row):
    return bool(row.imdb_id) and row.omdb_checked_at is None and bool(_omdb_key())


def _enrich_omdb(row):
    if not row.imdb_id:
        return
    d = _omdb(i=row.imdb_id, plot='short')
    if not d:
        return
    try:
        row.imdb_rating = float(d['imdbRating']) if d.get('imdbRating') not in (None, 'N/A') else None
    except ValueError:
        row.imdb_rating = None
    votes = (d.get('imdbVotes') or '').replace(',', '')
    row.imdb_votes = int(votes) if votes.isdigit() else None
    row.rt_score = None
    for r in d.get('Ratings', []):
        if r.get('Source') == 'Rotten Tomatoes':
            row.rt_score = r.get('Value')
    row.omdb_checked_at = utcnow()


def _apply_tmdb(row, d):
    row.tmdb_id = d['id']
    row.imdb_id = d.get('imdb_id') or None
    row.title = (d.get('title') or d.get('original_title') or row.title or 'Untitled')[:255]
    row.year = _year(d.get('release_date'))
    row.overview = d.get('overview') or row.overview
    row.tagline = (d.get('tagline') or '')[:500]
    row.runtime = d.get('runtime') or None
    row.original_language = (d.get('original_language') or '')[:8]
    row.poster_path = d.get('poster_path') or row.poster_path
    row.backdrop_path = d.get('backdrop_path')
    row.tmdb_rating = d.get('vote_average')
    row.tmdb_votes = d.get('vote_count')
    row.popularity = d.get('popularity')
    row.genres = ','.join(GENRE_ALIASES.get(g['name'], g['name']) for g in d.get('genres', []))
    credits = d.get('credits') or {}
    row.cast_json = json.dumps([
        {'name': c['name'], 'character': c.get('character', ''), 'photo': c.get('profile_path')}
        for c in credits.get('cast', [])[:10]])
    row.director = ', '.join(c['name'] for c in credits.get('crew', [])
                             if c.get('job') == 'Director')[:255]
    row.keywords = '|'.join(k['name'] for k in (d.get('keywords') or {}).get('keywords', [])[:25])
    trailers = [v for v in (d.get('videos') or {}).get('results', [])
                if v.get('site') == 'YouTube' and v.get('type') == 'Trailer']
    trailers.sort(key=lambda v: not v.get('official'))
    row.trailer_key = trailers[0]['key'] if trailers else None
    row.fetched_at = utcnow()


def _find_tmdb_id(ref):
    if ref.startswith('tmdb_'):
        return int(ref[5:]) if ref[5:].isdigit() else None
    if ref.startswith('tt') and ref[2:].isdigit():
        ref = ref[2:]
    if ref.isdigit():
        d = _tmdb(f'/find/tt{ref.zfill(7)}', external_source='imdb_id')
        results = (d or {}).get('movie_results') or []
        return results[0]['id'] if results else None
    # Legacy ids such as 'omdb_the_matrix' -> search by title.
    title = ref[5:].replace('_', ' ') if ref.startswith('omdb_') else ref
    d = _tmdb('/search/movie', query=title, include_adult='false')
    results = (d or {}).get('results') or []
    return results[0]['id'] if results else None


def _omdb_only_row(ref, row):
    """Fallback when TMDB is unavailable."""
    if ref.startswith('tt') and ref[2:].isdigit():
        ref = ref[2:]
    if ref.isdigit():
        d = _omdb(i=f'tt{ref.zfill(7)}', plot='short')
    else:
        d = _omdb(t=ref[5:].replace('_', ' ') if ref.startswith('omdb_') else ref, plot='short')
    digits = _imdb_digits((d or {}).get('imdbID'))
    if not d or not digits:
        return None
    row = db.session.get(MovieCache, digits) or MovieCache(movie_id=digits)
    row.imdb_id = d['imdbID']
    row.title = (d.get('Title') or 'Untitled')[:255]
    row.year = (d.get('Year') or '')[:4]
    row.overview = d.get('Plot') if d.get('Plot') != 'N/A' else None
    row.genres = ','.join(g.strip() for g in (d.get('Genre') or '').split(',') if g.strip() and g != 'N/A')
    row.director = d.get('Director') if d.get('Director') != 'N/A' else None
    row.poster_path = d.get('Poster') if d.get('Poster') not in (None, 'N/A') else row.poster_path
    row.fetched_at = utcnow()
    return row


def _refresh(ref, row, enrich):
    try:
        tmdb_id = row.tmdb_id if row is not None and row.tmdb_id else _find_tmdb_id(ref)
        data = _tmdb(f'/movie/{tmdb_id}', append_to_response='credits,keywords,videos',
                     language='en-US') if tmdb_id else None
        if data:
            canonical = _imdb_digits(data.get('imdb_id')) or f'tmdb_{data["id"]}'
            if row is None or row.movie_id != canonical:
                row = db.session.get(MovieCache, canonical) or MovieCache(movie_id=canonical)
            _apply_tmdb(row, data)
        else:
            row = _omdb_only_row(ref, row)
            if row is None:
                return None
        if enrich:
            _enrich_omdb(row)
        db.session.add(row)
        db.session.commit()
        return row
    except IntegrityError:
        db.session.rollback()  # another request cached the same movie first
        return _cache_lookup(ref)
    except Exception:
        log.exception('Refreshing movie %s failed', ref)
        db.session.rollback()
        return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def get_movie(ref, *, enrich=True, force=False):
    """ref = canonical id ('0133093'), 'tmdb_603', 'tt0133093', or a legacy 'omdb_*' id.
    Returns the movie dict (see movie_to_dict) or None. Falls back to a stale cached
    copy when upstream APIs fail, so pages never go blank."""
    ref = str(ref or '').strip()
    if not ref:
        return None
    row = _cache_lookup(ref)
    fresh = row is not None and row.fetched_at and utcnow() - row.fetched_at < CACHE_TTL
    if row is not None and fresh and not force:
        if enrich and _needs_omdb(row):
            try:
                _enrich_omdb(row)
                db.session.commit()
            except Exception:
                db.session.rollback()
        return movie_to_dict(row)
    refreshed = _refresh(ref, row, enrich)
    row = refreshed or row
    return movie_to_dict(row) if row is not None else None


def ensure_row(ref):
    d = get_movie(ref, enrich=False)
    return db.session.get(MovieCache, d['id']) if d else None


@ttl_cache(600)
def search_movies(query, limit=8):
    q = (query or '').strip()
    if len(q) < 2:
        return []
    data = _tmdb('/search/movie', query=q, include_adult='false', page=1)
    if data is not None:
        out = []
        for m in data.get('results', [])[:limit]:
            card = _tmdb_card(m)
            card['ref'] = card['id']
            card['overview'] = (m.get('overview') or '')[:160]
            out.append(card)
        return out
    data = _omdb(s=q, type='movie')  # TMDB down or not configured
    out = []
    for m in (data or {}).get('Search', [])[:limit]:
        digits = _imdb_digits(m.get('imdbID'))
        if digits:
            out.append({'id': digits, 'ref': digits, 'title': m.get('Title'),
                        'year': m.get('Year', 'N/A'), 'rating': 'N/A', 'genres': [], 'overview': '',
                        'poster': m['Poster'] if m.get('Poster', 'N/A') != 'N/A' else PLACEHOLDER_POSTER})
    return out


@ttl_cache(3600)
def get_trending(limit=12):
    data = _tmdb('/trending/movie/week')
    if data and data.get('results'):
        return [_tmdb_card(m) for m in data['results'][:limit]]
    # TMDB unavailable: show the most popular movies we already have cached.
    rows = MovieCache.query.order_by(MovieCache.popularity.desc()).limit(limit).all()
    return [movie_card(r) for r in rows]


def tmdb_ids_from(path, pages=3, **params):
    """Used by scripts/seed_catalog.py to collect TMDB ids from list/discover endpoints."""
    ids = []
    for page in range(1, pages + 1):
        data = _tmdb(path, page=page, **params)
        if not data:
            break
        ids.extend(m['id'] for m in data.get('results', []))
        if page >= data.get('total_pages', 1):
            break
    return ids


def tmdb_recommendations(tmdb_id, limit=10):
    data = _tmdb(f'/movie/{tmdb_id}/recommendations')
    return [_tmdb_card(m) for m in (data or {}).get('results', [])[:limit]]


# --------------------------------------------------------------------------
# Discover (filters) and the platform list
# --------------------------------------------------------------------------

GENRE_IDS = {name: gid for gid, name in TMDB_GENRES.items()}
_SORTS = {
    'popular': ('popularity.desc', {}),
    'top': ('vote_average.desc', {'vote_count.gte': 500}),
    'new': ('primary_release_date.desc', {'vote_count.gte': 30}),
}


@ttl_cache(300)
def _discover_raw(params_items):
    return _tmdb('/discover/movie', **dict(params_items))


def discover(genre=None, sort='popular', year=None, provider=None, free=False, country='IN', page=1):
    sort_by, extra = _SORTS.get(sort, _SORTS['popular'])
    params = {'sort_by': sort_by, 'include_adult': 'false', 'page': max(1, min(int(page or 1), 50)), **extra}
    if genre in GENRE_IDS:
        params['with_genres'] = GENRE_IDS[genre]
    if year and str(year).isdigit():
        params['primary_release_year'] = int(year)
    if sort == 'new':
        params['primary_release_date.lte'] = date.today().isoformat()
    if provider and str(provider).isdigit():
        params['with_watch_providers'] = int(provider)
        params['watch_region'] = country
    if free:
        params['with_watch_monetization_types'] = 'free|ads'
        params['watch_region'] = country
    data = _discover_raw(tuple(sorted(params.items())))
    if not data:
        return {'results': [], 'page': params['page'], 'total_pages': 0}
    return {'results': [_tmdb_card(m) for m in data.get('results', [])],
            'page': data.get('page', params['page']),
            'total_pages': min(data.get('total_pages', 1), 50)}


@ttl_cache(86400)
def provider_list(country='IN', limit=10):
    data = _tmdb('/watch/providers/movie', watch_region=country)
    items = (data or {}).get('results') or []
    items.sort(key=lambda p: (p.get('display_priorities') or {}).get(country, 999))
    return [{'id': p['provider_id'], 'name': p['provider_name'], 'logo': img(p.get('logo_path'), 'w92')}
            for p in items[:limit]]


@ttl_cache(86400)
def region_list():
    """Every country TMDB has streaming data for (used by the country dropdowns)."""
    data = _tmdb('/watch/providers/regions')
    items = (data or {}).get('results') or []
    return sorted(({'code': r['iso_3166_1'], 'name': r.get('english_name') or r['iso_3166_1']} for r in items),
                  key=lambda r: r['name'])
