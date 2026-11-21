"""One-off cleanup: removes ratings, watchlist items and likes that belong to users who no longer exist.

Before the account check was added, a user deleted by an admin could keep using their open
session and create new rows. On MySQL the foreign keys block that, on SQLite they do not.

    python scripts/cleanup_orphans.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scripts._boot  # noqa: F401,E402
from app import app  # noqa: E402
from models import MovieRating, ReviewLike, User, UserPref, Watchlist, db  # noqa: E402

with app.app_context():
    users = [u[0] for u in db.session.query(User.id).all()]
    ratings = MovieRating.query.filter(~MovieRating.user_id.in_(users)).delete(synchronize_session=False)
    marks = Watchlist.query.filter(~Watchlist.user_id.in_(users)).delete(synchronize_session=False)
    prefs = UserPref.query.filter(~UserPref.user_id.in_(users)).delete(synchronize_session=False)
    likes = ReviewLike.query.filter(~ReviewLike.user_id.in_(users)).delete(synchronize_session=False)
    live = [r[0] for r in db.session.query(MovieRating.id).all()]
    likes += ReviewLike.query.filter(~ReviewLike.rating_id.in_(live)).delete(synchronize_session=False)
    db.session.commit()
    print(f"removed: {ratings} ratings, {marks} watchlist items, {prefs} prefs, {likes} likes")
