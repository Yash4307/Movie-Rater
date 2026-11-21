"""Request-time recommendations. No sklearn here: similarities are precomputed offline
(scripts/build_similarity.py) into movie_similarity, so this is just a few SQL reads."""
from collections import defaultdict

from models import MovieCache, MovieRating, MovieSimilarity, db
from services import movies

# Chosen from the offline evaluation (scripts/evaluate.py): "people who liked X also liked Y" neighbours
# clearly beat content similarity, so the blend leans collaborative. Content still matters because it is
# the only signal for movies nobody in the rating data has seen (new releases, regional films).
CONTENT_WEIGHT = 0.3
COLLAB_WEIGHT = 1 - CONTENT_WEIGHT
FRESH_EVERY = 3        # every 3rd recommendation is a "fresh pick" judged by content only
# "Movies like THIS one" should look like it, so content leads there; co-liked neighbours add variety.
# (The evaluation measures taste-based recommendations, not this list, hence the different weights.)
SIMILAR_CONTENT_WEIGHT = 0.7
NEUTRAL = 5.5          # ratings are 1-10; at/below this a movie pushes recs away


def _fmt_stars(rating10):
    stars = rating10 / 2
    return f'{stars:g}'


def interleave(main, fresh, every=3):
    """main[0], main[1], fresh[0], main[2], main[3], fresh[1], ... (every-th slot from `fresh`)."""
    out, mi, fi = [], 0, 0
    while mi < len(main) or fi < len(fresh):
        for _ in range(every - 1):
            if mi < len(main):
                out.append(main[mi]); mi += 1
        if fi < len(fresh):
            out.append(fresh[fi]); fi += 1
    return out


def _cards(ids):
    rows = {r.movie_id: r for r in MovieCache.query.filter(MovieCache.movie_id.in_(ids)).all()} if ids else {}
    return [(i, movies.movie_card(rows[i])) for i in ids if i in rows]


def taste_profile(user_id, top=5):
    ratings = MovieRating.query.filter_by(user_id=user_id).all()
    weights = defaultdict(float)
    for r in ratings:
        w = (r.rating - NEUTRAL) / 4.5
        for g in (r.genres or '').split(','):
            if g.strip():
                weights[g.strip()] += w
    liked = sorted(((g, round(w, 2)) for g, w in weights.items() if w > 0), key=lambda x: -x[1])[:top]
    return {'rated': len(ratings),
            'average_stars': round(sum(r.rating for r in ratings) / len(ratings) / 2, 2) if ratings else None,
            'top_genres': [{'genre': g, 'score': w} for g, w in liked]}


def _popular_in_genres(genres, exclude, limit):
    out = []
    for g in genres:
        rows = (MovieCache.query.filter(MovieCache.genres.like(f'%{g}%'))
                .order_by(MovieCache.tmdb_rating.desc()).limit(limit * 3).all())
        for r in rows:
            if r.movie_id not in exclude and all(r.movie_id != o['id'] for o in out):
                card = movies.movie_card(r)
                card['reason'] = f'Top rated in {g}'
                out.append(card)
    return out


def recommend_for_user(user_id, limit=12):
    ratings = MovieRating.query.filter_by(user_id=user_id).all()
    rated = {r.movie_id: r for r in ratings}
    if not rated:
        top = MovieCache.query.order_by(MovieCache.tmdb_rating.desc(), MovieCache.popularity.desc()).limit(limit).all()
        return [dict(movies.movie_card(r), reason='Popular with everyone') for r in top]

    weights = {mid: (r.rating - NEUTRAL) / 4.5 for mid, r in rated.items()}
    denom = sum(abs(w) for w in weights.values()) or 1.0
    content, collab = defaultdict(float), defaultdict(float)
    because = {}
    rows = MovieSimilarity.query.filter(MovieSimilarity.movie_id.in_(list(weights))).all()
    for s in rows:
        if s.similar_id in rated:
            continue
        contrib = weights[s.movie_id] * s.score
        (content if s.source == 'content' else collab)[s.similar_id] += contrib / denom
        if contrib > because.get(s.similar_id, (0, None))[0]:
            because[s.similar_id] = (contrib, s.movie_id)

    # Movies with collaborative evidence are scored by the blend. Movies without any can only be judged by
    # content, so they are ranked among themselves and slotted in regularly instead of being buried.
    covered = {c: CONTENT_WEIGHT * content[c] + COLLAB_WEIGHT * collab[c] for c in collab}
    fresh = {c: v for c, v in content.items() if c not in collab}
    main_ids = [c for c, s in sorted(covered.items(), key=lambda kv: -kv[1]) if s > 0]
    fresh_ids = [c for c, s in sorted(fresh.items(), key=lambda kv: -kv[1]) if s > 0]
    ids = interleave(main_ids, fresh_ids, FRESH_EVERY)[:limit * 2]

    out = []
    for cid, card in _cards(ids):
        src = because.get(cid)
        if src and src[1] in rated:
            r = rated[src[1]]
            card['reason'] = f'Because you rated {r.movie_title} {_fmt_stars(r.rating)}\u2605'
        else:
            card['reason'] = 'Matches your taste'
        out.append(card)
        if len(out) >= limit:
            break

    if len(out) < limit:  # catalog too small / new user: fill from favourite genres
        top_genres = [g['genre'] for g in taste_profile(user_id)['top_genres'][:3]]
        exclude = set(rated) | {o['id'] for o in out}
        out.extend(_popular_in_genres(top_genres, exclude, limit)[:limit - len(out)])
    return out


def similar_to(movie_id, limit=10):
    """Content neighbours (lead) blended with 'people who liked this also liked' neighbours."""
    score = defaultdict(float)
    for r in MovieSimilarity.query.filter_by(movie_id=movie_id).all():
        score[r.similar_id] += (SIMILAR_CONTENT_WEIGHT if r.source == 'content' else 1 - SIMILAR_CONTENT_WEIGHT) * r.score
    best = [i for i, _ in sorted(score.items(), key=lambda kv: -kv[1])[:limit]]
    cards = [c for _, c in _cards(best)]
    if cards:
        return cards
    row = movies.ensure_row(movie_id)  # not in the model yet: ask TMDB live
    return movies.tmdb_recommendations(row.tmdb_id, limit) if row and row.tmdb_id else []
