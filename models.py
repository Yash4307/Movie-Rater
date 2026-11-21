"""Database models. Existing tables are unchanged; new tables are added at the bottom."""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Existing tables (unchanged)
# --------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())


class MovieRating(db.Model):
    __tablename__ = 'movie_ratings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    movie_id = db.Column(db.String(20), nullable=False)
    movie_title = db.Column(db.String(255), nullable=False)
    poster_url = db.Column(db.String(500))
    genres = db.Column(db.String(500))
    rating = db.Column(db.Integer, nullable=False)  # 1-10 (UI shows 5 stars)
    review = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    __table_args__ = (db.UniqueConstraint('user_id', 'movie_id', name='uq_rating_user_movie'),)

    user = db.relationship('User', backref=db.backref('ratings', lazy=True))


class Watchlist(db.Model):
    __tablename__ = 'watchlist'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    movie_id = db.Column(db.String(20), nullable=False)
    movie_title = db.Column(db.String(255), nullable=False)
    poster_url = db.Column(db.String(500))
    added_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    __table_args__ = (db.UniqueConstraint('user_id', 'movie_id', name='uq_watch_user_movie'),)


class ReviewLike(db.Model):
    __tablename__ = 'review_likes'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rating_id = db.Column(db.Integer, db.ForeignKey('movie_ratings.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    __table_args__ = (db.UniqueConstraint('user_id', 'rating_id', name='uq_user_rating_like'),)


# --------------------------------------------------------------------------
# New tables (created automatically by db.create_all(); nothing to ALTER)
# --------------------------------------------------------------------------

class UserPref(db.Model):
    """Per-account settings (currently the light/dark theme). Separate table so the
    existing users table never needs an ALTER."""
    __tablename__ = 'user_prefs'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    theme = db.Column(db.String(10), default='dark')

class MovieCache(db.Model):
    """One row per movie. movie_id is the same key MovieRating.movie_id already uses:
    the IMDb id without 'tt' (e.g. '0133093'), or 'tmdb_<id>' when TMDB has no IMDb id."""
    __tablename__ = 'movie_cache'
    movie_id = db.Column(db.String(20), primary_key=True)
    tmdb_id = db.Column(db.Integer, index=True)
    imdb_id = db.Column(db.String(12), index=True)
    title = db.Column(db.String(255), nullable=False)
    year = db.Column(db.String(4))
    overview = db.Column(db.Text)
    tagline = db.Column(db.String(500))
    genres = db.Column(db.String(500))            # comma separated
    keywords = db.Column(db.Text)                 # '|' separated
    director = db.Column(db.String(255))
    cast_json = db.Column(db.Text)                # JSON list of {name, character, photo}
    runtime = db.Column(db.Integer)
    original_language = db.Column(db.String(8))
    poster_path = db.Column(db.String(500))       # TMDB path ('/abc.jpg') or a full URL
    backdrop_path = db.Column(db.String(255))
    trailer_key = db.Column(db.String(20))        # YouTube video id
    imdb_rating = db.Column(db.Float)
    imdb_votes = db.Column(db.Integer)
    rt_score = db.Column(db.String(8))
    tmdb_rating = db.Column(db.Float)
    tmdb_votes = db.Column(db.Integer)
    popularity = db.Column(db.Float)
    fetched_at = db.Column(db.DateTime, default=utcnow)
    omdb_checked_at = db.Column(db.DateTime)


class WatchProviderCache(db.Model):
    __tablename__ = 'watch_provider_cache'
    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.String(20), nullable=False)
    country = db.Column(db.String(2), nullable=False)
    payload = db.Column(db.Text, nullable=False)  # JSON from TMDB for that country
    fetched_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint('movie_id', 'country', name='uq_provider_movie_country'),)


class MovieSimilarity(db.Model):
    """Precomputed offline by scripts/build_similarity.py; the web app only reads it."""
    __tablename__ = 'movie_similarity'
    id = db.Column(db.Integer, primary_key=True)
    movie_id = db.Column(db.String(20), nullable=False, index=True)
    similar_id = db.Column(db.String(20), nullable=False)
    score = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(10), nullable=False)  # 'content' | 'collab'
    __table_args__ = (db.UniqueConstraint('movie_id', 'similar_id', 'source', name='uq_similarity_pair'),)
