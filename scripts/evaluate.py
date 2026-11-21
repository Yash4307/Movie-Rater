"""Offline evaluation: does the recommender beat just recommending popular movies?

    python scripts/evaluate.py --movielens data/ml-latest-small

Method (5-fold, leave-one-out hit rate, fully repeatable):
  * MovieLens users are split into folds. For each fold the collaborative model is trained on the OTHER
    folds' users, so no test user ever influences the model that is tested on them.
  * for each test user we hide one movie they loved (4+ stars), build their taste profile from the rest
    exactly like recs.py does (rating -> weight, similarity rows cut to the stored top-20), and do this
    for up to 3 different hidden movies per user.
  * HitRate@10 = how often the hidden movie appears in the top 10. Every method ranks the SAME pool
    (movies with enough training ratings), because only those can be verified by MovieLens.
  * numbers come with a 95% margin of error; the 'vs popularity' block is a paired comparison.
Content similarity does not use ratings, so it needs no split."""
import argparse
import os
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_similarity import (CONTENT_MIN, COLLAB_MIN, TOP_K, app, collab_matrix,  # noqa: E402
                                      content_matrix, read_movielens, truncate_topk)
from models import MovieCache  # noqa: E402
from services.recs import CONTENT_WEIGHT, NEUTRAL  # noqa: E402

OLD_CONTENT_WEIGHT = 0.7   # the first version of the live blend: content-heavy, rating-centred collaborative


NAMES = ('random', 'popularity', 'content', 'collab', 'collab_likes', 'hybrid_old', 'hybrid')


def evaluate(movielens, folds=5, holdouts=3, k=10, seed=7, max_ml_users=None, min_pool_ratings=5):
    rows = MovieCache.query.order_by(MovieCache.movie_id).all()
    ids = [r.movie_id for r in rows]
    index = {m: i for i, m in enumerate(ids)}
    triples = read_movielens(movielens, {r.tmdb_id: r.movie_id for r in rows if r.tmdb_id}, max_users=max_ml_users)
    by_user = defaultdict(dict)
    for u, m, r in triples:
        by_user[u][m] = r
    users = sorted(u for u, d in by_user.items() if len(d) >= 5 and any(r >= 8 for r in d.values()))
    if len(users) < 40:
        raise SystemExit(f'Only {len(users)} usable MovieLens users overlap your catalog. '
                         'Seed a bigger catalog (python scripts/train.py --seed --seed-pages 25) and retry.')
    rng = random.Random(seed)
    rng.shuffle(users)
    fold_of = {u: i % folds for i, u in enumerate(users)}

    S_content = truncate_topk(content_matrix(rows), TOP_K, CONTENT_MIN)
    per_user = {n: [] for n in NAMES}
    pool_sizes = []
    for f in range(folds):
        test = [u for u in users if fold_of[u] == f]                       # sorted: repeatable
        test_set = set(test)
        train = [t for t in triples if t[0] not in test_set]
        popularity = np.zeros(len(ids))
        for _, m, _ in train:
            popularity[index[m]] += 1
        pool = popularity >= min_pool_ratings
        centered, likes = collab_matrix(ids, train), collab_matrix(ids, train, mode='likes')
        if centered is None or likes is None:
            continue
        S_collab, S_likes = truncate_topk(centered, TOP_K, COLLAB_MIN), truncate_topk(likes, TOP_K, COLLAB_MIN)
        S_hybrid_old = OLD_CONTENT_WEIGHT * S_content + (1 - OLD_CONTENT_WEIGHT) * S_collab   # first live blend
        S_hybrid = CONTENT_WEIGHT * S_content + (1 - CONTENT_WEIGHT) * S_likes                # live blend (recs.py)
        pool_sizes.append(int(pool.sum()))
        for u in test:
            ratings = by_user[u]
            liked = sorted(m for m, r in ratings.items() if r >= 8 and pool[index[m]])
            acc, used = {n: 0.0 for n in NAMES}, 0
            for held in rng.sample(liked, min(holdouts, len(liked))):
                profile = {m: r for m, r in ratings.items() if m != held}
                if len(profile) < 4:
                    continue
                pi = np.array([index[m] for m in profile])
                w = np.array([(r - NEUTRAL) / 4.5 for r in profile.values()])
                cand = pool.copy()
                cand[pi] = False                                          # never recommend what they already rated
                scores = {'popularity': popularity, 'content': w @ S_content[pi], 'collab': w @ S_collab[pi],
                          'collab_likes': w @ S_likes[pi], 'hybrid_old': w @ S_hybrid_old[pi],
                          'hybrid': w @ S_hybrid[pi]}
                target = index[held]
                for name, s in scores.items():
                    s = np.where(cand, np.asarray(s, dtype=float), -np.inf)
                    acc[name] += int(target in np.argpartition(-s, k - 1)[:k])
                acc['random'] += k / cand.sum()
                used += 1
            if used:
                for n in NAMES:
                    per_user[n].append(acc[n] / used)
    if not per_user['popularity']:
        raise SystemExit('Not enough training ratings for the collaborative model.')
    arr = {n: np.array(v) for n, v in per_user.items()}
    n_users = len(arr['popularity'])
    se = lambda x: 1.96 * x.std(ddof=1) / np.sqrt(len(x))
    return {'users_tested': n_users, 'catalog': len(ids), 'pool': int(np.mean(pool_sizes)), 'k': k, 'folds': folds,
            'hit_rate': {n: round(float(a.mean()), 3) for n, a in arr.items()},
            'ci': {n: round(float(se(a)), 3) for n, a in arr.items()},
            'vs_popularity': {n: (round(float((a - arr['popularity']).mean()), 3),
                                  round(float(se(a - arr['popularity'])), 3)) for n, a in arr.items() if n != 'popularity'}}


def format_results(r):
    out = [f"{r['users_tested']} users x {r['folds']}-fold, ranking among ~{r['pool']} movies that MovieLens users rated",
           f"HitRate@{r['k']} (with 95% margin of error):"]
    for name, v in sorted(r['hit_rate'].items(), key=lambda kv: kv[1]):
        out.append(f"  {name:<17}{v:>6.1%} +/- {r['ci'][name]:.1%}")
    out.append('Difference vs popularity (paired; a result is only clearly better if it is bigger than its +/-):')
    for name, (d, ci) in sorted(r['vs_popularity'].items(), key=lambda kv: -kv[1][0]):
        if name != 'random':
            out.append(f"  {name:<17}{d:>+7.1%} +/- {ci:.1%}")
    return '\n'.join(out)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--movielens', required=True)
    ap.add_argument('--folds', type=int, default=5)
    ap.add_argument('--holdouts', type=int, default=3, help='hidden movies tested per user')
    a = ap.parse_args()
    with app.app_context():
        print('\n' + format_results(evaluate(a.movielens, folds=a.folds, holdouts=a.holdouts)))
