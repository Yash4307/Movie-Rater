# MovieRater

A full-stack movie discovery and rating app: search any movie, see where to stream it, rate and review it, and get recommendations from a hybrid recommender that is evaluated offline.

**Live demo:** https://movie-rater-3y6w.onrender.com/ ·  **Stack:** Flask, SQLAlchemy, MySQL-compatible database (TiDB Cloud), scikit-learn, vanilla JS

<!-- Add screenshots to docs/screenshots/ and link them here: login, dashboard, movie dialog, discover, admin. -->

## Features

- **Accounts:** register / log in with email or username, "remember me", per-account light/dark theme, delete-your-account, admin role with a dashboard (stats, activity, rating distribution, user management).
- **Search and movie details:** live search (TMDB), poster, cast, trailer link, IMDb / Rotten Tomatoes scores (OMDb), community rating.
- **Where to watch:** per-country streaming availability (TMDB / JustWatch data) with free options first, links to each platform, and a watchlist helper that finds the fewest subscriptions covering your list. Prices are deliberately not shown: plan tiers change often and the data doesn't say which plan includes a title.
- **Ratings and reviews:** 5-star ratings, written reviews with a sentiment label (VADER), likes, community feed, public profiles.
- **Discover:** filter by genre, year, platform and "free to watch".
- **Recommendations:** "For You" and "You might also like" from a hybrid recommender (below).

## How the recommender works

Trained offline (`scripts/train.py`), stored in the database, and only *read* by the web app, so the free web server never trains anything or imports scikit-learn.

- **Content model:** TF-IDF over genres, keywords, director/cast and plot, compared with cosine similarity. Works for every movie, including new releases.
- **Collaborative model:** "people who rated both movies 4+ stars" item-item similarity, learned from your users' ratings plus the MovieLens `ml-latest-small` dataset (so it works before the site has many users).
- **Blend:** collaborative-heavy for personal recommendations (30% content / 70% co-liked). Movies with no collaborative data (new releases, regional films) are ranked by content and slotted in every third place so they are not buried. "Movies like this one" leads with content (70/30) so it looks like the movie.
- **Evaluation** (`scripts/evaluate.py`): 5-fold, leave-one-out. For each held-out user, hide one loved movie and check whether it appears in the top 10. Every method ranks the same candidate pool, and results come with a 95% margin of error.

| Method | HitRate@10 |
|---|---|
| random | 3.7% |
| content only | 26.8% |
| first blend (rating-centred collaborative) | 31.3% |
| rating-centred collaborative | 33.7% |
| **popularity baseline** | **36.4%** |
| co-liked collaborative | 46.8% |
| **current blend** | **47.3%** |

_545 MovieLens users, about 309 candidate movies, margin of error about ±2.7 points._ The blend beats the popularity baseline by about 11 points; the earlier blend did not. Limits: the test can only judge movies MovieLens has ratings for, so it says little about brand-new releases.

## Tech stack

Flask 3 · SQLAlchemy 2 · Alembic (Flask-Migrate) · PyMySQL · gunicorn · WhiteNoise · vanilla JavaScript · scikit-learn / SciPy (offline training only) · VADER sentiment · pytest · GitHub Actions · Render · TiDB Cloud (MySQL-compatible) · SQLite for local development.

## Project structure

```
app.py               routes, auth, API, startup (applies migrations)
models.py            database models
security.py          rate limiting, security headers, database TLS options
services/            movies.py (TMDB/OMDb + cache), providers.py (where to watch),
                     recs.py (recommendations), sentiment.py
scripts/             train.py, build_similarity.py, evaluate.py, seed_catalog.py
migrations/          Alembic migrations (see MIGRATIONS.md)
templates/, static/  UI (no build step)
tests/               pytest suite
```

## Run locally (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env        # then fill in SECRET_KEY and the API keys
python app.py                 # http://localhost:5001  (SQLite by default)
```

Tests: `python -m pip install -r requirements-ml.txt pytest` then `python -m pytest -q`.

### Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | required; signs sessions |
| `DATABASE_URL` | `sqlite:///local_test.db` or `mysql://USER:PASSWORD@HOST:4000/DBNAME` (URL-encode special characters in the password) |
| `TMDB_API_KEY` | movie data, trending, discover, streaming availability (free key at themoviedb.org) |
| `OMDB_API_KEY` | optional: IMDb / Rotten Tomatoes scores |
| `APP_NAME` | brand name (default `MovieRater`) |
| `DB_SSL_CA`, `DB_SSL_VERIFY`, `DB_SSL` | database TLS: verified against public CAs by default; custom CA file; `DB_SSL_VERIFY=0` encrypted but unverified; `DB_SSL=0` no TLS (local only) |
| `HEALTHCHECK_TOKEN` | optional: lets an uptime pinger use `/healthz?db=1&token=...` to keep the database awake |

## Train the recommender

```powershell
python -m pip install -r requirements-ml.txt
# download ml-latest-small from https://grouplens.org/datasets/movielens/ and unzip it to data/ml-latest-small
python scripts/train.py --seed --seed-pages 25 --movielens data/ml-latest-small --evaluate
```

It writes to whatever `DATABASE_URL` points at, so run it against the database the deployed site uses. A scheduled GitHub Actions job (`.github/workflows/rebuild-recs.yml`) can do this weekly; it needs the repository secrets `DATABASE_URL`, `TMDB_API_KEY`, `OMDB_API_KEY` and `SECRET_KEY`.

## Deploy (Render + TiDB Cloud)

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60`
- **Environment:** `PYTHON_VERSION=3.11.9`, `SECRET_KEY` (generate with `python -c "import secrets; print(secrets.token_hex(32))"`), `DATABASE_URL`, `TMDB_API_KEY`, `OMDB_API_KEY`, `APP_NAME`.
- The app applies database migrations automatically on start, so no shell is needed. Free instances sleep when idle, so the first request after a quiet period is slow.

## Security

CSRF protection on every state-changing request, salted password hashes, login / sign-up / search rate limiting, strict Content-Security-Policy and other security headers, request-size limit, verified database TLS, role checks re-validated on every request, ORM-only queries, escaped output. Dependencies are audited in CI (`pip-audit`).

## Data sources and credits

- This product uses the TMDB API but is not endorsed or certified by TMDB. Streaming availability data is provided by JustWatch. Ratings from OMDb.
- Recommendation training uses the MovieLens dataset (not included in this repository; downloaded separately under its licence): F. Maxwell Harper and Joseph A. Konstan. 2015. *The MovieLens Datasets: History and Context.* ACM Transactions on Interactive Intelligent Systems 5(4), Article 19. https://doi.org/10.1145/2827872

## License

See [LICENSE](LICENSE).
