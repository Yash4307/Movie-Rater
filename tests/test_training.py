"""End-to-end test of the training pipeline on synthetic data (no network, no real MovieLens)."""
import csv
import os
import random
import sys

os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app as appmod
from models import MovieCache, MovieRating, MovieSimilarity, User, db
from scripts import build_similarity as bs
from scripts import evaluate as ev
from services import recs

CLUSTERS = {
    'space': (['Sci-Fi'], ['spaceship', 'alien', 'galaxy', 'astronaut', 'planet']),
    'love': (['Romance'], ['wedding', 'kiss', 'heartbreak', 'couple', 'paris']),
    'crime': (['Crime'], ['heist', 'detective', 'gangster', 'murder', 'police']),
    'toon': (['Animation'], ['talking', 'toys', 'musical', 'princess', 'friendship']),
}
PER = 30


def build_catalog():
    rng = random.Random(3)
    movies, idx = {}, 0
    for name, (genres, words) in CLUSTERS.items():
        for i in range(PER):
            overview = ' '.join(rng.choices(words, k=7) + rng.choices(['story', 'journey', 'life', 'world'], k=3))
            db.session.add(MovieCache(movie_id=f'{idx:07d}', tmdb_id=1000 + idx, title=f'{name.title()} Film {i}',
                                      year=str(1990 + i % 30), genres=','.join(genres), overview=overview,
                                      keywords='|'.join(rng.sample(words, 3)), original_language='en',
                                      tmdb_rating=6 + (i % 4), popularity=float(PER - i)))
            movies[f'{idx:07d}'] = name
            idx += 1
    db.session.commit()
    return movies


def write_movielens(folder, n_users=200):
    rng = random.Random(5)
    names = list(CLUSTERS)
    with open(folder / 'links.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['movieId', 'imdbId', 'tmdbId'])
        for i in range(len(names) * PER):
            w.writerow([i, f'{i:07d}', 1000 + i])
        w.writerow([9999, '0000000', ''])                       # MovieLens rows without a TMDB id exist
    with open(folder / 'ratings.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['userId', 'movieId', 'rating', 'timestamp'])
        for u in range(n_users):
            fav = names[u % len(names)]
            base = names.index(fav) * PER
            for m in rng.sample(range(base, base + PER), 12):
                w.writerow([u, m, rng.choice([4.0, 4.5, 5.0]), 0])
            for m in rng.sample([i for i in range(len(names) * PER) if not base <= i < base + PER], 4):
                w.writerow([u, m, rng.choice([1.0, 1.5, 2.0]), 0])
            w.writerow([u, 9999, 3.0, 0])


@pytest.fixture()
def world(tmp_path):
    with appmod.app.app_context():
        db.drop_all(); db.create_all()
        clusters = build_catalog()
        write_movielens(tmp_path)
        yield clusters, str(tmp_path)


def test_pipeline_learns_the_clusters(world):
    clusters, ml = world
    stats = bs.run(movielens=ml)
    assert stats['catalog'] == 120 and stats['content_pairs'] > 0 and stats['collab_pairs'] > 0
    assert stats['ml_users'] == 200 and stats['ml_movies'] == 120          # unmapped MovieLens row ignored
    some = next(m for m, c in clusters.items() if c == 'crime')
    top = [r.similar_id for r in MovieSimilarity.query.filter_by(movie_id=some, source='content')
           .order_by(MovieSimilarity.score.desc()).limit(10)]
    assert sum(clusters[t] == 'crime' for t in top) >= 8                    # content model finds same-genre films
    top = [c['id'] for c in recs.similar_to(some, 10)]
    assert sum(clusters[t] == 'crime' for t in top) >= 8                    # blended neighbours too


def test_recommendations_follow_a_users_taste(world):
    clusters, ml = world
    bs.run(movielens=ml)
    love = [m for m, c in clusters.items() if c == 'love']
    db.session.add(User(username='u', email='u@x.com', password='x')); db.session.commit()
    for m in love[:6]:
        db.session.add(MovieRating(user_id=1, movie_id=m, movie_title='t', rating=10, genres='Romance'))
    db.session.commit()
    out = recs.recommend_for_user(1, limit=10)
    assert sum(clusters[o['id']] == 'love' for o in out) >= 8
    assert all(o['reason'].startswith('Because you rated') for o in out[:3])


def test_evaluation_beats_popularity(world):
    _, ml = world
    r = ev.evaluate(ml)
    hr = r['hit_rate']
    assert r['users_tested'] >= 150 and r['folds'] == 5
    assert hr['hybrid'] > hr['popularity'] + 0.2 and hr['collab'] > hr['random'] * 3
    assert hr['content'] > hr['popularity'] and hr['collab_likes'] > hr['popularity'] + 0.2 and r['pool'] == 120


def test_no_ratings_means_content_only_and_stale_collab_is_cleared(world):
    _, ml = world
    bs.run(movielens=ml)
    assert MovieSimilarity.query.filter_by(source='collab').count() > 0
    stats = bs.run()                                                        # no MovieLens, no user ratings
    assert stats['collab_pairs'] == 0 and MovieSimilarity.query.filter_by(source='collab').count() == 0
    assert stats['content_pairs'] > 0


def test_missing_movielens_files_give_a_clear_error(world, tmp_path):
    with pytest.raises(FileNotFoundError, match='ratings.csv'):
        bs.read_movielens(str(tmp_path / 'nope'), {})


def test_evaluation_is_repeatable_and_reports_margins(world):
    _, ml = world
    a, b = ev.evaluate(ml), ev.evaluate(ml)
    assert a == b                                                           # same data -> same numbers, every run
    assert all(0 < v < 0.2 for n, v in a['ci'].items() if n != 'random')   # margins are reported and sane
    d, ci = a['vs_popularity']['hybrid']
    assert d > ci                                                           # clearly better than popularity
    assert 'hybrid_old' in ev.format_results(a) and '+/-' in ev.format_results(a)


def test_collab_mode_flag_changes_what_is_stored(world):
    _, ml = world
    bs.run(movielens=ml, collab_mode='centered')
    centered = {(r.movie_id, r.similar_id): r.score for r in MovieSimilarity.query.filter_by(source='collab')}
    bs.run(movielens=ml, collab_mode='likes')
    likes = {(r.movie_id, r.similar_id): r.score for r in MovieSimilarity.query.filter_by(source='collab')}
    assert centered and likes and centered != likes


def test_fresh_picks_are_not_buried(world):
    """Movies with no collaborative data still get every 3rd slot (new releases, regional films)."""
    clusters, _ = world
    ids = sorted(clusters)[:9]
    me = ids[0]
    db.session.add(User(username='u', email='u@x.com', password='x')); db.session.commit()
    db.session.add(MovieRating(user_id=1, movie_id=me, movie_title='t', rating=10, genres='x'))
    for i, s in zip(ids[1:5], (.9, .8, .7, .6)):                        # four co-liked neighbours
        db.session.add(MovieSimilarity(movie_id=me, similar_id=i, score=s, source='collab'))
    for i, s in zip(ids[5:7], (.9, .8)):                                 # two content-only neighbours
        db.session.add(MovieSimilarity(movie_id=me, similar_id=i, score=s, source='content'))
    db.session.commit()
    out = [o['id'] for o in recs.recommend_for_user(1, limit=6)]
    assert out == [ids[1], ids[2], ids[5], ids[3], ids[4], ids[6]]
    assert recs.interleave(['a', 'b', 'c'], [], 3) == ['a', 'b', 'c'] and recs.interleave([], ['x', 'y'], 3) == ['x', 'y']


def test_similar_movies_lead_with_content_neighbours(world):
    clusters, _ = world
    me, c1, c2, k1, k2 = sorted(clusters)[:5]
    for i, s in ((c1, .5), (c2, .4)):
        db.session.add(MovieSimilarity(movie_id=me, similar_id=i, score=s, source='content'))
    for i, s in ((k1, .6), (k2, .5)):                                    # co-liked neighbours score higher raw
        db.session.add(MovieSimilarity(movie_id=me, similar_id=i, score=s, source='collab'))
    db.session.commit()
    assert [c['id'] for c in recs.similar_to(me, 4)] == [c1, c2, k1, k2]
