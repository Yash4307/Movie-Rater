# MovieRater upgrade: data layer, where-to-watch, ML recommendations

Drop these files into your repo (keep your `templates/` and existing `static/`). Your current
front-end keeps working: `/search_movie`, `/search_suggestions`, `/rate_movie`,
`/add_to_watchlist` return the same JSON shape as before, now with more fields.

## Why data was failing (found in the old app.py)
1. `get_trending_movies()` and `fetch_movies_by_genre()` called OMDb up to 10 times per page load
   (about 20-30 calls per homepage view). OMDb free is ~1,000/day, so the quota ran out and
   posters, ratings and summaries came back empty.
2. Cinemagoer scrapes IMDb HTML, which breaks often and is blocked on many cloud IPs.
3. `/search_suggestions` hit IMDb on every keystroke.
4. Movie ids were inconsistent (IMDb number, `omdb_title`, or none for trending).

## What changed
- `services/movies.py`: TMDB primary, OMDb only for IMDb/RT ratings, MySQL cache (7 days), stale
  copy if APIs fail, placeholder poster, API keys never logged. Cinemagoer removed.
- Old rating ids (IMDb number like `0133093`) still work; they map to TMDB through `/find`.
- `services/providers.py`: where to watch (TMDB/JustWatch), free first, cheapest known subscription,
  and a watchlist optimizer ("which subscriptions cover most of my watchlist").
- `services/recs.py` + `scripts/`: hybrid recommender (content TF-IDF + item-item collaborative),
  trained offline, stored in `movie_similarity`. Web process never imports sklearn.
- `services/sentiment.py`: VADER sentiment on reviews.
- Security: salted password hashes (old SHA-256 hashes upgrade on next login), fixed the
  `delete_user` crash when the user had likes, generic error messages instead of raw exceptions.
- DB: Aiven `mysql://...?ssl-mode=REQUIRED` URLs are accepted as-is; optional verified TLS.

## Setup
1. Get a free TMDB key (themoviedb.org > Settings > API). Add `TMDB_API_KEY` in Render env vars.
2. Deploy. New tables are created automatically on boot; no ALTER needed.
3. Seed + train from your own machine (Render free has no shell). In your local `.env` set
   `DATABASE_URL` to the Aiven URL, then:
   ```
   pip install -r requirements-ml.txt
   python scripts/seed_catalog.py
   python scripts/build_similarity.py
   ```
   Re-run weekly, or let `.github/workflows/rebuild-recs.yml` do it (add the 4 secrets).
4. Fill `data/plan_prices.json` (`monthly_inr`) from each platform's pricing page. I left them
   `null` on purpose: prices change and I could not verify current ones.
5. Render start command: `gunicorn app:app --workers 1 --threads 4 --timeout 60` (matches Procfile).
6. Optional: ping `https://<your-app>/healthz` every 10 min (cron-job.org). `?db=1` also touches MySQL.

## New endpoints (for the dynamic UI)
`GET /api/search?q=` | `/api/movie/<id>` | `/api/movie/<id>/where-to-watch?country=IN` |
`/api/movie/<id>/similar` | `/api/movie/<id>/reviews` | `/api/recommendations` | `/api/taste` |
`/api/trending` | `/api/watchlist/where-to-watch` | `POST /remove_from_watchlist` | `GET /healthz`

## Notes
- TMDB requires JustWatch attribution: show `attribution` from the where-to-watch responses.
- Check TMDB's terms if you ever monetize. Check Aiven's current free-plan limits and idle policy.
- Not done yet (needs your templates): CSRF protection, admin actions as POST instead of GET links,
  and the UI itself. Send `templates/` + `static/` plus your reference images when ready.


---

# Part 2: UI redesign (MovieRater, red accent, dark + light)

Replace these files (all of them, they depend on each other):
`templates/` (base, auth_base, _scene, _macros, login, register, index, profile, admin) and
`static/style.css`, `static/script.js`. `static/img/no-poster.svg` is the poster fallback.

## What you get
- Solid surfaces, one red accent, poster-first layout; no glassmorphism. Dark and light themes
  (toggle in the top bar, remembered in the browser).
- Login/register: animated cinema scene (projector with spinning reels, flickering light beam,
  scrolling film strips, floating rating/review/watchlist cards, clapperboard, popcorn).
  Pure CSS/SVG, no images; respects "reduce motion".
- Dashboard with sidebar (Home, Discover, My Watchlist, My Ratings, Community, Profile),
  hero carousel, trending, community top rated, your stats, "For You" with reasons.
- Movie dialog for every poster: scores, cast, trailer, where to watch (free first, cheapest known
  plan), rate/review, community reviews with sentiment, similar movies.
- Discover: filter by genre, platform, year, sort, and "Free to watch" (live TMDB).
- Watchlist: "Find the cheapest way to watch my list".
- Admin: stats, recent activity, ratings distribution, user management.

## Behaviour changes to know about
- Login accepts email OR username (the field is now called `identifier`), plus "Remember me".
- Registration requires a 3-30 char username and an 8+ char password. There is no "Full name"
  field because the database has no column for it. Google/GitHub login and "Forgot password" are
  not included (no email service, and you asked for the existing login only).
- CSRF protection is on for every POST. Admin actions are now POST forms (they were GET links).
- The brand name comes from `APP_NAME` (default `MovieRater`).
- Search matches movie titles only (TMDB), so the placeholder says "Search movies...".
