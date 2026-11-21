"""Offline smoke test: fake TMDB/OMDb responses + SQLite. Run: pytest -q"""
import os
import sys

os.environ.update(SECRET_KEY='test', DATABASE_URL='sqlite:///:memory:',
                  TMDB_API_KEY='tmdb-test', OMDB_API_KEY='omdb-test')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app as appmod
from models import MovieCache, MovieRating, MovieSimilarity, db
from security import RateLimiter
from services import movies


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise movies.requests.HTTPError()


def film(i, title, imdb, genres, overview):
    return {'id': i, 'imdb_id': imdb, 'title': title, 'release_date': '2010-01-01', 'overview': overview,
            'genres': [{'name': g} for g in genres], 'poster_path': f'/p{i}.jpg', 'vote_average': 7.5,
            'vote_count': 1000, 'popularity': 50, 'original_language': 'en', 'runtime': 120,
            'credits': {'cast': [{'name': f'Actor {i}', 'profile_path': None}],
                        'crew': [{'job': 'Director', 'name': 'Dir A'}]},
            'keywords': {'keywords': [{'name': 'space'}, {'name': 'future'}]},
            'videos': {'results': [{'site': 'YouTube', 'type': 'Trailer', 'key': 'abc', 'official': True}]}}


FILMS = {1: film(1, 'Space One', 'tt0000001', ['Science Fiction'], 'astronauts travel through space'),
         2: film(2, 'Space Two', 'tt0000002', ['Science Fiction'], 'astronauts explore deep space'),
         3: film(3, 'Love Story', 'tt0000003', ['Romance'], 'two people fall in love in paris'),
         4: film(4, 'Space Three', 'tt0000004', ['Science Fiction'], 'space crew future war')}


def fake_get(url, params=None, headers=None, timeout=None):
    if 'themoviedb' in url:
        if url.endswith('/search/movie'):
            return FakeResp({'results': [{'id': 1, 'title': 'Space One', 'release_date': '2010-01-01',
                                          'poster_path': '/p1.jpg', 'vote_average': 7.5, 'genre_ids': [878]}]})
        if '/find/' in url:
            n = int(url.split('tt')[-1])
            return FakeResp({'movie_results': [{'id': n}]})
        if url.endswith('/watch/providers'):
            return FakeResp({'results': {'IN': {'link': 'x', 'flatrate': [
                {'provider_id': 8, 'provider_name': 'Netflix', 'logo_path': '/n.png'}]}}})
        if url.endswith('/trending/movie/week'):
            return FakeResp({'results': [{'id': 1, 'title': 'Space One', 'poster_path': '/p1.jpg', 'backdrop_path': '/b1.jpg',
                                          'overview': 'astronauts travel', 'release_date': '2010-01-01',
                                          'vote_average': 7.5, 'genre_ids': [878]}]})
        if url.endswith('/discover/movie'):
            return FakeResp({'page': 1, 'total_pages': 3, 'results': [
                {'id': 2, 'title': 'Space Two', 'poster_path': '/p2.jpg', 'release_date': '2011-01-01', 'vote_average': 6.9, 'genre_ids': [878]}]})
        if url.endswith('/watch/providers/regions'):
            return FakeResp({'results': [{'iso_3166_1': 'IN', 'english_name': 'India'},
                                         {'iso_3166_1': 'NZ', 'english_name': 'New Zealand'},
                                         {'iso_3166_1': 'AR', 'english_name': 'Argentina'}]})
        if url.endswith('/watch/providers/movie'):
            return FakeResp({'results': [{'provider_id': 8, 'provider_name': 'Netflix', 'logo_path': '/n.png',
                                          'display_priorities': {'IN': 1}}]})
        mid = int(url.rsplit('/', 1)[-1])
        return FakeResp(FILMS[mid]) if mid in FILMS else FakeResp({}, 404)
    return FakeResp({'Response': 'True', 'imdbRating': '8.1', 'imdbVotes': '1,234',
                     'Ratings': [{'Source': 'Rotten Tomatoes', 'Value': '90%'}]})


def login(c, ident, pw):
    """Logs in through the real form (with CSRF) and arms the client with a fresh token header."""
    c.environ_base.pop('HTTP_X_CSRF_TOKEN', None)
    c.get('/login')
    with c.session_transaction() as sess:
        tok = sess['_csrf']
    resp = c.post('/login', data={'identifier': ident, 'password': pw, 'csrf_token': tok})
    c.get('/')  # renders a page, which mints the post-login CSRF token
    with c.session_transaction() as sess:
        c.environ_base['HTTP_X_CSRF_TOKEN'] = sess.get('_csrf', '')
    return resp


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(movies._http, 'get', fake_get)
    with appmod.app.app_context():
        db.drop_all(); db.create_all()
        appmod.limiter = RateLimiter()          # every test starts with fresh rate-limit counters
        u = appmod.User(username='a', email='a@x.com', password=appmod.hash_password('pw'))
        db.session.add(u); db.session.commit()
    c = appmod.app.test_client()
    login(c, 'a', 'pw')
    return c


def test_movie_fetch_and_cache(client):
    d = client.get('/api/movie/tmdb_1').get_json()
    assert d['id'] == '0000001' and d['genres'] == ['Sci-Fi'] and d['imdb_rating'] == '8.1'
    assert d['rt_score'] == '90%' and d['trailer_url'].endswith('abc')
    # legacy cinemagoer-style id resolves through /find
    assert client.get('/api/movie/0000002').get_json()['title'] == 'Space Two'


def test_legacy_search_and_rate(client):
    m = client.post('/search_movie', json={'title': 'space'}).get_json()
    assert m['title'] == 'Space One' and m['poster'].endswith('/p1.jpg')
    assert client.post('/rate_movie', json={'movie_id': m['id'], 'rating': 9, 'review': 'Loved it!'}).status_code == 200
    assert client.post('/rate_movie', json={'movie_id': m['id'], 'rating': 99}).status_code == 400
    r = client.get(f"/api/movie/{m['id']}/reviews").get_json()
    assert r['reviews'][0]['sentiment'] == 'positive'


def test_where_to_watch_and_watchlist(client):
    w = client.get('/api/movie/tmdb_3/where-to-watch').get_json()
    assert w['flatrate'][0]['name'] == 'Netflix' and 'JustWatch' in w['attribution']
    client.post('/add_to_watchlist', json={'movie_id': 'tmdb_3'})
    opt = client.get('/api/watchlist/where-to-watch').get_json()
    assert opt['plan'][0]['provider'] == 'Netflix'


def test_recommendations_pipeline(client):
    with appmod.app.app_context():
        for i in FILMS:
            movies.get_movie(f'tmdb_{i}', enrich=False)
        import scripts.build_similarity as bs
        assert bs.run()['content_pairs'] > 0
        db.session.add(MovieRating(user_id=1, movie_id='0000001', movie_title='Space One', rating=10, genres='Sci-Fi'))
        db.session.commit()
    recs = client.get('/api/recommendations').get_json()
    assert recs and recs[0]['title'].startswith('Space') and 'Because you rated' in recs[0]['reason']
    assert client.get('/api/taste').get_json()['top_genres'][0]['genre'] == 'Sci-Fi'


def test_old_sha256_password_upgrades(client):
    import hashlib
    with appmod.app.app_context():
        db.session.add(appmod.User(username='old', email='o@x.com',
                                   password=hashlib.sha256(b'secret').hexdigest()))
        db.session.commit()
    c = appmod.app.test_client()
    assert login(c, 'old', 'secret').status_code == 302
    with appmod.app.app_context():
        assert not appmod.User.query.filter_by(username='old').first().password.startswith(hashlib.sha256(b'secret').hexdigest())


def test_pages_render(client):
    for path in ['/', '/user/a']:
        r = client.get(path)
        assert r.status_code == 200, path
    home = client.get('/').get_data(as_text=True)
    assert 'hero-slide' in home and 'Space One' in home and 'view-discover' in home
    assert 'csrf-token' in home
    anon = appmod.app.test_client()
    for path in ['/login', '/register']:
        html = anon.get(path).get_data(as_text=True)
        assert 'class="scene' in html and 'csrf_token' in html


def test_login_by_email_and_csrf(client):
    c = appmod.app.test_client()
    assert c.post('/login', data={'identifier': 'a', 'password': 'pw'}).status_code == 400   # no CSRF token
    assert login(c, 'A@X.COM', 'pw').status_code == 302                                         # email, any case
    c2 = appmod.app.test_client()
    c2.get('/login')
    assert c2.post('/rate_movie', json={'movie_id': 'x', 'rating': 5}).status_code == 400        # API needs the header


def test_discover_and_providers(client):
    d = client.get('/api/discover?genre=Sci-Fi&sort=top&provider=8&free=1').get_json()
    assert d['results'][0]['title'] == 'Space Two' and d['total_pages'] == 3
    assert client.get('/api/providers').get_json()[0]['name'] == 'Netflix'


def test_register_validation_and_admin(client):
    c = appmod.app.test_client()
    c.get('/register')
    with c.session_transaction() as sess:
        tok = sess['_csrf']
    r = c.post('/register', data={'username': 'bob', 'email': 'b@x.com', 'password': 'short', 'csrf_token': tok})
    assert r.status_code == 200 and b'at least 8' in r.data
    assert c.post('/register', data={'username': 'bob', 'email': 'b@x.com', 'password': 'longenough', 'csrf_token': tok}).status_code == 302
    with appmod.app.app_context():
        u = appmod.User.query.filter_by(username='a').first(); u.is_admin = True; db.session.commit()
    login(client, 'a', 'pw')
    page = client.get('/admin').get_data(as_text=True)
    assert 'Ratings distribution' in page and 'bob' in page
    assert client.get('/admin/delete_user/2').status_code == 405                                  # GET no longer deletes
    assert client.post('/admin/delete_user/2').status_code == 302
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username='bob').first() is None


def test_theme_is_per_account(client):
    assert 'data-theme="dark"' in client.get('/').get_data(as_text=True)
    assert client.post('/api/theme', json={'theme': 'light'}).status_code == 200
    assert client.post('/api/theme', json={'theme': 'neon'}).status_code == 400
    assert 'data-theme="light"' in client.get('/').get_data(as_text=True)
    # a different account in the same browser starts dark, and user 'a' still gets light back on login
    c2 = appmod.app.test_client()
    c2.get('/register')
    with c2.session_transaction() as sess:
        tok = sess['_csrf']
    c2.post('/register', data={'username': 'zed', 'email': 'z@x.com', 'password': 'longenough', 'csrf_token': tok})
    login(c2, 'zed', 'longenough')
    assert 'data-theme="dark"' in c2.get('/').get_data(as_text=True)
    login(client, 'a', 'pw')
    assert 'data-theme="light"' in client.get('/').get_data(as_text=True)
    # logged-out pages never inherit a user's theme
    assert 'data-theme="dark"' in appmod.app.test_client().get('/login').get_data(as_text=True)


def test_countries_list(client):
    names = [c['name'] for c in client.get('/api/countries').get_json()]
    assert names == ['Argentina', 'India', 'New Zealand']


def test_delete_account(client):
    client.post('/rate_movie', json={'movie_id': 'tmdb_1', 'rating': 8, 'review': 'good'})
    client.post('/add_to_watchlist', json={'movie_id': 'tmdb_3'})
    client.post('/api/theme', json={'theme': 'light'})
    with appmod.app.app_context():
        u = appmod.User.query.filter_by(username='a').first(); u.is_admin = True; db.session.commit()
    login(client, 'a', 'pw')
    # sole admin is protected
    r = client.post('/account/delete', data={'confirm_username': 'a'})
    assert r.status_code == 302
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username='a').first() is not None
        u = appmod.User.query.filter_by(username='a').first(); u.is_admin = False; db.session.commit()
    login(client, 'a', 'pw')
    # wrong username does nothing
    client.post('/account/delete', data={'confirm_username': 'A'})
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username='a').first() is not None
    # right username wipes everything and logs out
    r = client.post('/account/delete', data={'confirm_username': 'a'})
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    with appmod.app.app_context():
        assert appmod.User.query.filter_by(username='a').first() is None
        assert MovieRating.query.count() == 0 and appmod.Watchlist.query.count() == 0
        assert appmod.UserPref.query.count() == 0
    assert client.get('/').status_code == 302
    assert b'permanently deleted' in client.get('/login').data


def test_deleted_or_demoted_user_loses_access_immediately(client):
    # a second browser logged in as 'victim'
    with appmod.app.app_context():
        db.session.add(appmod.User(username='victim', email='v@x.com', password=appmod.hash_password('pw'), is_admin=True))
        db.session.commit()
    v = appmod.app.test_client()
    login(v, 'victim', 'pw')
    assert v.get('/admin').status_code == 200
    # demoted while logged in: admin page closes at once, no re-login needed
    with appmod.app.app_context():
        u = appmod.User.query.filter_by(username='victim').first(); u.is_admin = False; db.session.commit()
    assert v.get('/admin').status_code == 302
    # deleted while logged in: API calls get 401, pages redirect, and no ratings are written
    with appmod.app.app_context():
        u = appmod.User.query.filter_by(username='victim').first(); db.session.delete(u); db.session.commit()
    r = v.post('/rate_movie', json={'movie_id': 'tmdb_1', 'rating': 8, 'review': 'ghost'})
    assert r.status_code == 401
    assert v.get('/').status_code == 302
    with appmod.app.app_context():
        assert MovieRating.query.filter_by(review='ghost').count() == 0


def test_mixed_review_is_labelled_mixed():
    from services import sentiment
    text = "The first half was great, the acting was brilliant, but the second half was terrible and boring."
    assert sentiment.analyze(text)['label'] == 'mixed'
    assert sentiment.analyze('Loved it! Absolutely fantastic.')['label'] == 'positive'
    assert sentiment.analyze('Boring and slow. Disappointing.')['label'] == 'negative'


def test_where_to_watch_has_no_prices_and_summarises():
    from services import providers
    base = {'free': [], 'ads': [], 'flatrate': [], 'rent': [], 'buy': []}
    assert providers.summarize({**base, 'ads': [{'name': 'MX Player'}]})['text'] == 'Free on MX Player (with ads)'
    s = providers.summarize({**base, 'flatrate': [{'name': 'Netflix'}, {'name': 'JioHotstar'}]})
    assert s['kind'] == 'subscription' and 'Netflix' in s['text'] and '₹' not in s['text']
    assert providers.summarize({**base, 'rent': [{'name': 'Apple TV'}]})['kind'] == 'rent_buy'
    assert providers.summarize(base)['kind'] == 'unavailable'


def test_watchlist_optimizer_without_prices(client):
    client.post('/add_to_watchlist', json={'movie_id': 'tmdb_3'})
    r = client.get('/api/watchlist/where-to-watch').get_json()
    assert r['plan'][0]['provider'] == 'Netflix' and r['plan'][0]['titles'] == ['Love Story']
    assert 'monthly' not in r['plan'][0] and 'estimated_monthly_total' not in r
    assert r['rent_buy'] == [] and r['unavailable'] == [] and 'plan_note' in r


def test_platform_links():
    from services import providers
    assert providers.platform_url('Amazon Prime Video') == 'https://www.primevideo.com'
    assert providers.platform_url('JioHotstar') == 'https://www.hotstar.com'
    assert providers.platform_url('Apple TV Plus') == 'https://tv.apple.com'
    assert providers.platform_url('Vi movies and tv') == 'https://moviesandtv.myvi.in'
    assert providers.platform_url('Vi Movies & TV') == 'https://moviesandtv.myvi.in'
    assert providers.platform_url('Some Tiny Service') is None


def test_where_to_watch_includes_platform_urls(client):
    w = client.get('/api/movie/tmdb_3/where-to-watch').get_json()
    assert w['flatrate'][0]['name'] == 'Netflix' and w['flatrate'][0]['url'] == 'https://www.netflix.com'
    client.post('/add_to_watchlist', json={'movie_id': 'tmdb_3'})
    assert client.get('/api/watchlist/where-to-watch').get_json()['plan'][0]['url'] == 'https://www.netflix.com'


def test_admin_and_logout_sit_under_profile(client):
    with appmod.app.app_context():
        u = appmod.User.query.filter_by(username='a').first(); u.is_admin = True; db.session.commit()
    login(client, 'a', 'pw')
    html = client.get('/').get_data(as_text=True)
    assert 'sidebar-foot' not in html
    assert html.index('>Profile<') < html.index('>Admin<') < html.index('>Logout<')
    assert '>Logout<' in client.get('/admin').get_data(as_text=True)
