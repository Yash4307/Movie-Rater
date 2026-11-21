"""Where to watch: TMDB /watch/providers (JustWatch data).

We deliberately do NOT show prices. TMDB/JustWatch say which platform carries a title but not
which plan tier includes it, and plan prices change often. Users pick their own plan on the
platform's site."""
import json
import logging
from datetime import timedelta

from models import Watchlist, WatchProviderCache, db, utcnow
from services import movies
from services.movies import _tmdb, img

log = logging.getLogger(__name__)

TTL = timedelta(hours=24)
KINDS = ('free', 'ads', 'flatrate', 'rent', 'buy')
ATTRIBUTION = 'Streaming availability data provided by JustWatch'
PLAN_NOTE = 'Which plan you need varies by platform, so check the platform before subscribing.'

# Platform home pages (not movie pages: TMDB has no per-title links). Matched against the provider
# name with everything except letters and digits removed, first match wins. Unknown platforms
# simply show as plain, non-clickable chips.
PLATFORM_URLS = (
    ('netflix', 'https://www.netflix.com'),
    ('primevideo', 'https://www.primevideo.com'), ('amazonprime', 'https://www.primevideo.com'),
    ('amazonvideo', 'https://www.primevideo.com'), ('amazonminitv', 'https://www.amazon.in/minitv'),
    ('jiohotstar', 'https://www.hotstar.com'), ('hotstar', 'https://www.hotstar.com'),
    ('jiocinema', 'https://www.hotstar.com'),
    ('zee5', 'https://www.zee5.com'), ('sonyliv', 'https://www.sonyliv.com'),
    ('appletv', 'https://tv.apple.com'), ('youtube', 'https://www.youtube.com/movies'),
    ('googleplay', 'https://play.google.com/store/movies'), ('mxplayer', 'https://www.mxplayer.in'),
    ('lionsgateplay', 'https://www.lionsgateplay.com'), ('mubi', 'https://mubi.com'),
    ('hoichoi', 'https://www.hoichoi.tv'), ('sunnxt', 'https://www.sunnxt.com'),
    ('aha', 'https://www.aha.video'), ('erosnow', 'https://erosnow.com'),
    ('discoveryplus', 'https://www.discoveryplus.in'), ('shemaroome', 'https://www.shemaroome.com'),
    ('disneyplus', 'https://www.disneyplus.com'), ('hulu', 'https://www.hulu.com'),
    ('peacock', 'https://www.peacocktv.com'), ('paramountplus', 'https://www.paramountplus.com'),
    ('crunchyroll', 'https://www.crunchyroll.com'), ('tubi', 'https://tubitv.com'),
    ('vimoviesandtv', 'https://moviesandtv.myvi.in'), ('airtelxstream', 'https://www.airtelxstream.in'),
    ('manoramamax', 'https://www.manoramamax.com'), ('hungama', 'https://www.hungama.com'),
)


def platform_url(name):
    key = ''.join(ch for ch in (name or '').lower().replace('&', 'and') if ch.isalnum())
    for needle, url in PLATFORM_URLS:
        if needle in key:
            return url
    return None


def _normalize(raw):
    out = {'link': (raw or {}).get('link'), **{k: [] for k in KINDS}}
    for kind in KINDS:
        seen = set()
        for p in (raw or {}).get(kind, []):
            if p['provider_id'] in seen:
                continue
            seen.add(p['provider_id'])
            out[kind].append({'id': p['provider_id'], 'name': p['provider_name'],
                              'logo': img(p.get('logo_path'), 'w92'), 'url': platform_url(p['provider_name'])})
    return out


def _fetch_country(row, country):
    """Cached per (movie, country) for 24h in MySQL."""
    cached = WatchProviderCache.query.filter_by(movie_id=row.movie_id, country=country).first()
    if cached and utcnow() - cached.fetched_at < TTL:
        return json.loads(cached.payload)
    data = _tmdb(f'/movie/{row.tmdb_id}/watch/providers') if row.tmdb_id else None
    if data is None:
        return json.loads(cached.payload) if cached else {}  # stale beats nothing
    raw = (data.get('results') or {}).get(country, {})
    try:
        if cached is None:
            cached = WatchProviderCache(movie_id=row.movie_id, country=country, payload='{}')
            db.session.add(cached)
        cached.payload = json.dumps(raw)
        cached.fetched_at = utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.warning('Could not cache providers for %s', row.movie_id)
    return raw


def _names(providers):
    return ', '.join(p['name'] for p in providers)


def summarize(options):
    """One-line answer, best case first: free, then subscription, then rent/buy."""
    free = options['free'] + options['ads']
    if free:
        suffix = '' if options['free'] else ' (with ads)'
        return {'kind': 'free', 'text': f'Free on {_names(free)}{suffix}', 'providers': [p['name'] for p in free]}
    if options['flatrate']:
        return {'kind': 'subscription', 'text': f'Streaming on {_names(options["flatrate"])}',
                'providers': [p['name'] for p in options['flatrate']]}
    if options['rent'] or options['buy']:
        return {'kind': 'rent_buy', 'text': f'Not on a subscription here. Rent or buy on {_names(options["rent"] + options["buy"])}.'}
    return {'kind': 'unavailable', 'text': 'Not currently streaming in this country.'}


def get_watch_options(ref, country='IN'):
    row = movies.ensure_row(ref)
    if row is None:
        return None
    options = _normalize(_fetch_country(row, country))
    return {'movie_id': row.movie_id, 'title': row.title, 'country': country, **options,
            'summary': summarize(options), 'plan_note': PLAN_NOTE, 'attribution': ATTRIBUTION}


def optimize_watchlist(user_id, country='IN', cap=25):
    """Fewest subscriptions that cover the most of this user's watchlist (greedy set cover)."""
    items = (Watchlist.query.filter_by(user_id=user_id)
             .order_by(Watchlist.added_at.desc()).limit(cap).all())
    free_titles, coverage, titles, rent_buy, unavailable = [], {}, {}, [], []
    for it in items:
        opts = get_watch_options(it.movie_id, country)
        if opts is None:
            unavailable.append(it.movie_title)
            continue
        titles[opts['movie_id']] = opts['title']
        if opts['free'] or opts['ads']:
            free_titles.append(opts['title'])
        elif opts['flatrate']:
            for p in opts['flatrate']:
                coverage.setdefault(p['name'], set()).add(opts['movie_id'])
        elif opts['rent'] or opts['buy']:
            rent_buy.append({'title': opts['title'],
                             'providers': [p['name'] for p in (opts['rent'] + opts['buy'])][:4]})
        else:
            unavailable.append(opts['title'])

    remaining = {mid for ids in coverage.values() for mid in ids}
    plan = []
    while remaining:
        # most uncovered titles first; ties broken alphabetically so results are stable
        name, ids = min(coverage.items(), key=lambda kv: (-len(kv[1] & remaining), kv[0]))
        gain = ids & remaining
        if not gain:
            break
        plan.append({'provider': name, 'url': platform_url(name), 'titles': sorted(titles[m] for m in gain)})
        remaining -= gain
    return {'country': country, 'free': sorted(free_titles), 'plan': plan,
            'rent_buy': sorted(rent_buy, key=lambda r: r['title']), 'unavailable': sorted(unavailable),
            'plan_note': PLAN_NOTE, 'attribution': ATTRIBUTION}
