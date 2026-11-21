"""One command to train the recommender and see what it learned.

    pip install -r requirements-ml.txt
    python scripts/train.py --seed                                  # catalog + content model
    python scripts/train.py --seed --seed-pages 25 --movielens data/ml-latest-small --evaluate

--seed          fill movie_cache from TMDB first (needs TMDB_API_KEY; a few minutes)
--movielens DIR optional pretraining data: a downloaded MovieLens 'ml-latest-small' folder
--evaluate      also print HitRate@10 against a popularity baseline (needs --movielens)
Run it locally with DATABASE_URL pointing at the database the website uses (SQLite or Aiven)."""
import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_similarity import app, run  # noqa: E402
from models import MovieCache, db  # noqa: E402
from services import recs  # noqa: E402


def report(stats):
    print('\n=== Training report ===')
    print(f"catalog: {stats['catalog']} movies")
    print(f"content model : {stats['content_pairs']} similar-movie links, covering "
          f"{stats['content_covered']}/{stats['catalog']} movies")
    src = f"{stats['app_ratings']} ratings from your users"
    if stats.get('ml_ratings'):
        src += f" + {stats['ml_ratings']} MovieLens ratings ({stats['ml_users']} users, {stats['ml_movies']} catalog movies)"
    print(f"collab model  : {stats['collab_pairs']} links, covering {stats['collab_covered']} movies  [{src}]")
    if not stats['collab_pairs']:
        print('  (no collaborative links yet: needs 2+ users with 2+ ratings each, or --movielens)')


def examples(n=4):
    pool = [r.movie_id for r in MovieCache.query.order_by(MovieCache.popularity.desc()).limit(40).all()]
    print('\n=== Sample neighbours ===')
    for mid in random.Random(1).sample(pool, min(n, len(pool))):
        row = db.session.get(MovieCache, mid)
        similar = [c['title'] for c in recs.similar_to(mid, 5)]
        print(f'{row.title} ({row.year}) ->', ', '.join(similar) or '(none)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seed', action='store_true')
    ap.add_argument('--seed-pages', type=int, default=10)
    ap.add_argument('--movielens')
    ap.add_argument('--max-ml-users', type=int)
    ap.add_argument('--collab-mode', choices=['centered', 'likes'], default='likes',
                    help="'likes' (default, best in the evaluation) = movies co-liked at 4+ stars; "
                         "'centered' = rating-centred similarity")
    ap.add_argument('--evaluate', action='store_true')
    a = ap.parse_args()
    t0 = time.time()
    with app.app_context():
        if a.seed:
            from scripts.seed_catalog import seed
            seed(a.seed_pages)
        stats = run(a.movielens, a.max_ml_users, a.collab_mode)
        report(stats)
        examples()
        if a.evaluate:
            if not a.movielens:
                print('\n--evaluate needs --movielens')
            else:
                from scripts.evaluate import evaluate, format_results
                print('\n=== Evaluation ===')
                print(format_results(evaluate(a.movielens, max_ml_users=a.max_ml_users)))
    print(f'\nfinished in {time.time() - t0:.0f}s. Restart nothing: the website reads the new rows immediately.')
