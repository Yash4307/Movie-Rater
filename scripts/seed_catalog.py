"""Fill movie_cache with a catalog the recommender can learn from.

Run LOCALLY (Render's free plan has no shell). Point DATABASE_URL in your local .env at Aiven.
    python scripts/seed_catalog.py              # ~600 movies, a few minutes
    python scripts/seed_catalog.py --pages 20   # bigger catalog
Uses TMDB only (no OMDb quota); IMDb ratings get filled in lazily when a movie is viewed.
"""
import argparse
import time

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scripts._boot  # noqa: F401  (loads .env, fixes sys.path)
from app import app
from models import MovieCache, MovieRating, Watchlist, db
from services import movies

INDIAN_LANGS = ['hi', 'ta', 'te', 'ml', 'kn', 'bn', 'mr']


def seed(pages=10):
    """Fills movie_cache from TMDB lists, then makes sure every movie your users touched is cached.
    Must run inside app.app_context()."""
    if not movies.tmdb_enabled():
        raise SystemExit('TMDB_API_KEY is not set.')
    wanted = []
    wanted += movies.tmdb_ids_from('/movie/popular', pages)
    wanted += movies.tmdb_ids_from('/movie/top_rated', pages)
    for lang in INDIAN_LANGS:
        wanted += movies.tmdb_ids_from('/discover/movie', max(2, pages // 4),
                                       with_original_language=lang, sort_by='popularity.desc',
                                       **{'vote_count.gte': 100})
    have = {r[0] for r in MovieCache.query.with_entities(MovieCache.tmdb_id).all()}
    todo = [i for i in dict.fromkeys(wanted) if i not in have]
    print(f'{len(have)} cached, {len(todo)} to fetch')
    for n, tmdb_id in enumerate(todo, 1):
        movies.get_movie(f'tmdb_{tmdb_id}', enrich=False)
        if n % 25 == 0:
            print(f'  {n}/{len(todo)}')
        time.sleep(0.05)

    ids = {r[0] for r in MovieRating.query.with_entities(MovieRating.movie_id).all()}
    ids |= {r[0] for r in Watchlist.query.with_entities(Watchlist.movie_id).all()}
    missing = [i for i in ids if db.session.get(MovieCache, i) is None]
    print(f'{len(missing)} user movies not cached yet')
    for i in missing:
        movies.get_movie(i, enrich=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pages', type=int, default=10, help='pages per TMDB list (20 movies each)')
    args = ap.parse_args()
    with app.app_context():
        seed(args.pages)
    print('done. Now run: python scripts/train.py')


if __name__ == '__main__':
    main()
