"""Train the recommender offline and store the results in MySQL (movie_similarity).

    pip install -r requirements-ml.txt
    python scripts/train.py                       # normal entry point (adds a report)
    python scripts/build_similarity.py            # just this step

content : TF-IDF over genres / keywords / director+cast / overview -> cosine similarity.
          Works from day one and needs no ratings.
collab  : item-item similarity on mean-centred ratings. Ratings come from your own users and,
          optionally, a MovieLens download (--movielens), which gives real "people who liked X
          also liked Y" signal even while your own user base is tiny.
The web app never trains anything; it only reads the rows saved here.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scripts._boot  # noqa: F401,E402
from app import app  # noqa: E402
from models import MovieCache, MovieRating, MovieSimilarity, db  # noqa: E402

TOP_K = 20                 # similar movies stored per movie, per source
CONTENT_MIN = 0.08         # ignore weaker content matches
COLLAB_MIN = 0.05
BLOCK_WEIGHTS = {'genres': 0.20, 'keywords': 0.25, 'people': 0.15, 'overview': 0.40}
COLLAB_SHRINK = 5.0        # pairs backed by few shared raters are pulled toward zero
MAX_DENSE_ITEMS = 6000     # n x n matrices; keep the catalog below this on a laptop


def _cast(row):
    try:
        return json.loads(row.cast_json) if row.cast_json else []
    except ValueError:
        return []


def content_matrix(rows):
    """n x n content similarity (float32, zero diagonal) for the given MovieCache rows."""
    n = len(rows)
    if n < 3:
        raise ValueError('Need at least 3 movies in movie_cache; run scripts/seed_catalog.py first.')
    if n > MAX_DENSE_ITEMS:
        raise ValueError(f'{n} movies is more than {MAX_DENSE_ITEMS}; seed a smaller catalog.')

    def tokens(r):
        decade = f'decade:{r.year[:3]}0s' if r.year and r.year.isdigit() else ''
        lang = f'lang:{r.original_language}' if r.original_language else ''
        return '|'.join(t for t in [*(r.genres or '').split(','), lang, decade] if t)

    def block(texts, **kw):
        try:
            return TfidfVectorizer(**kw).fit_transform(texts)
        except ValueError:                      # a field that is empty for the whole catalog
            return sparse.csr_matrix((len(texts), 0))

    pipe = dict(token_pattern=r'[^|]+', lowercase=True)
    blocks = {
        'genres': block([tokens(r) for r in rows], **pipe),
        'keywords': block([r.keywords or '' for r in rows], **pipe),
        'people': block(['|'.join([r.director or ''] + [c['name'] for c in _cast(r)][:5]) for r in rows], **pipe),
        'overview': block([f'{r.title}. {r.overview or ""} {r.tagline or ""}' for r in rows],
                          stop_words='english', ngram_range=(1, 2), min_df=2,
                          sublinear_tf=True, max_features=30000),
    }
    # sqrt(weight) scaling makes the dot product a weighted sum of the per-block cosines
    X = sparse.hstack([np.sqrt(BLOCK_WEIGHTS[k]) * blocks[k] for k in BLOCK_WEIGHTS]).tocsr()
    S = (X @ X.T).toarray().astype(np.float32)
    np.fill_diagonal(S, 0)
    return S


def read_movielens(folder, tmdb_to_movie, max_users=None, min_ratings=5):
    """Reads a MovieLens 'latest' download (ratings.csv + links.csv) and keeps only movies that are
    in our catalog (matched through TMDB ids). Ratings (0.5-5 stars) are doubled to our 1-10 scale.
    Returns [(user, movie_id, rating10), ...]."""
    ratings_path, links_path = os.path.join(folder, 'ratings.csv'), os.path.join(folder, 'links.csv')
    for p in (ratings_path, links_path):
        if not os.path.exists(p):
            raise FileNotFoundError(f'{p} not found. Download ml-latest-small from grouplens.org/datasets/movielens '
                                    'and unzip it so that ratings.csv and links.csv are in that folder.')
    links = {}
    with open(links_path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if (r.get('tmdbId') or '').strip():
                links[r['movieId']] = int(float(r['tmdbId']))
    per_user = defaultdict(list)
    with open(ratings_path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            movie = tmdb_to_movie.get(links.get(r['movieId']))
            if movie:
                per_user[r['userId']].append((movie, float(r['rating']) * 2))
    users = [u for u, v in per_user.items() if len(v) >= min_ratings]
    if max_users:
        users = users[:max_users]
    return [(f'ml:{u}', m, r) for u in users for m, r in per_user[u]]


def app_ratings():
    """Ratings from your own users (already on the 1-10 scale)."""
    return [(f'app:{u}', m, float(r)) for u, m, r in
            db.session.query(MovieRating.user_id, MovieRating.movie_id, MovieRating.rating).all()]


LIKED = 8   # 4 stars or more (ratings are 1-10)


def collab_matrix(ids, triples, min_user_ratings=2, mode='centered'):
    """n x n item-item similarity aligned to `ids`; None if too little data.
    mode='centered': cosine on mean-centred ratings (uses dislikes too).
    mode='likes'   : cosine on 'rated 4+ stars' only (co-liked movies), which often ranks better for top-N."""
    index = {m: i for i, m in enumerate(ids)}
    by_user = defaultdict(dict)
    for u, m, r in triples:
        if m in index and (mode == 'centered' or r >= LIKED):
            by_user[u][m] = r
    by_user = {u: d for u, d in by_user.items() if len(d) >= min_user_ratings}
    if len(by_user) < 2:
        return None
    rr, cc, vals = [], [], []
    for ui, d in enumerate(by_user.values()):
        mean = sum(d.values()) / len(d)
        for m, r in d.items():
            rr.append(ui); cc.append(index[m]); vals.append(r - mean if mode == 'centered' else 1.0)
    shape = (len(by_user), len(ids))
    M = sparse.csr_matrix((vals, (rr, cc)), shape=shape)
    B = sparse.csr_matrix((np.ones(len(rr)), (rr, cc)), shape=shape)
    Mn = normalize(M, axis=0)
    S = (Mn.T @ Mn).toarray()
    C = (B.T @ B).toarray()                     # how many users rated both items
    S = S * (C / (C + COLLAB_SHRINK))
    S[C < 2] = 0                                # never trust a pair seen by a single user
    np.fill_diagonal(S, 0)
    return np.clip(S, 0, None).astype(np.float32)


def truncate_topk(S, k=TOP_K, min_score=0.0):
    """Keeps only each row's k best entries, exactly what ends up stored in movie_similarity."""
    out = np.zeros_like(S)
    k = min(k, S.shape[1] - 1)
    for i in range(S.shape[0]):
        idx = np.argpartition(-S[i], k - 1)[:k]
        keep = idx[S[i, idx] >= min_score]
        out[i, keep] = S[i, keep]
    return out


def top_pairs(ids, S, source, min_score):
    T = truncate_topk(S, TOP_K, min_score)
    rows, cols = np.nonzero(T)
    return [(ids[i], ids[j], float(T[i, j]), source) for i, j in zip(rows, cols)]


def save(source, pairs):
    MovieSimilarity.query.filter_by(source=source).delete()
    for i in range(0, len(pairs), 2000):
        db.session.bulk_insert_mappings(MovieSimilarity, [
            {'movie_id': a, 'similar_id': b, 'score': s, 'source': src} for a, b, s, src in pairs[i:i + 2000]])
    db.session.commit()


def run(movielens=None, max_ml_users=None, collab_mode='likes'):
    """Trains both similarity tables. Returns a dict of numbers for the report."""
    rows = MovieCache.query.all()
    ids = [r.movie_id for r in rows]
    stats = {'catalog': len(ids)}

    S_content = content_matrix(rows)
    pairs = top_pairs(ids, S_content, 'content', CONTENT_MIN)
    save('content', pairs)
    stats['content_pairs'] = len(pairs)
    stats['content_covered'] = len({a for a, *_ in pairs})

    triples = app_ratings()
    stats['app_ratings'] = len(triples)
    stats['ml_ratings'] = 0
    if movielens:
        tmdb_to_movie = {r.tmdb_id: r.movie_id for r in rows if r.tmdb_id}
        ml = read_movielens(movielens, tmdb_to_movie, max_users=max_ml_users)
        stats['ml_ratings'], stats['ml_users'] = len(ml), len({u for u, *_ in ml})
        stats['ml_movies'] = len({m for _, m, _ in ml})
        triples += ml
    S_collab = collab_matrix(ids, triples, mode=collab_mode)
    if S_collab is None:
        MovieSimilarity.query.filter_by(source='collab').delete()
        db.session.commit()
        stats['collab_pairs'] = stats['collab_covered'] = 0
    else:
        pairs = top_pairs(ids, S_collab, 'collab', COLLAB_MIN)
        save('collab', pairs)
        stats['collab_pairs'] = len(pairs)
        stats['collab_covered'] = len({a for a, *_ in pairs})
    return stats


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--movielens', help='folder containing MovieLens ratings.csv and links.csv')
    ap.add_argument('--max-ml-users', type=int, default=None)
    ap.add_argument('--collab-mode', choices=['centered', 'likes'], default='likes')
    a = ap.parse_args()
    with app.app_context():
        print(run(a.movielens, a.max_ml_users, a.collab_mode))
