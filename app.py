from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from imdb import IMDb
from dotenv import load_dotenv
import hashlib
import os
import requests
from whitenoise import WhiteNoise

# Load environment variables
load_dotenv()

# OMDb API Configuration
OMDB_API_KEY = os.getenv('OMDB_API_KEY','')
if not OMDB_API_KEY:
    raise RuntimeError('OMDB_API_KEY is not set. Add it to .env.')
OMDB_BASE_URL = 'https://www.omdbapi.com/'

app = Flask(__name__)
app.wsgi_app = WhiteNoise(app.wsgi_app, root='static/')

secret_key = os.getenv('SECRET_KEY', '')
if not secret_key:
    raise RuntimeError('SECRET_KEY is not set. Add it to .env.')
app.secret_key = secret_key

# Database configuration (MySQL)
database_url = os.getenv('DATABASE_URL')
if not database_url:
    db_user = os.getenv('DB_USER')
    db_password = os.getenv('DB_PASSWORD')
    db_host = os.getenv('DB_HOST', 'localhost')
    db_name = os.getenv('DB_NAME')
    # Grab Railway's custom internal port variable, default to 3306 locally
    db_port = os.getenv('DB_PORT', '3306')

    if not all([db_user, db_password, db_name]):
        raise RuntimeError('Database credentials are missing in .env')

    # Added the : {db_port} match to handle Railway's dynamic network routing
    database_url = f'mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Engine options to prevent MySQL connection timeouts in production
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "connect_args": {
        "ssl": {
            "ssl_mode": "REQUIRED"
        }
    }
}

db = SQLAlchemy(app)
migrate = Migrate(app, db)
imdb = IMDb()
TMDB_API_KEY = os.getenv('TMDB_API_KEY')

# --- Database Models ---

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
    poster_url = db.Column(db.String(500)) # NEW: Store poster for gallery view
    genres = db.Column(db.String(500))
    rating = db.Column(db.Integer, nullable=False)
    review = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    
    # Relationship to get username easily in community feed
    user = db.relationship('User', backref=db.backref('ratings', lazy=True))

class Watchlist(db.Model):
    __tablename__ = 'watchlist'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    movie_id = db.Column(db.String(20), nullable=False)
    movie_title = db.Column(db.String(255), nullable=False)
    poster_url = db.Column(db.String(500))
    added_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class ReviewLike(db.Model):
    __tablename__ = 'review_likes'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rating_id = db.Column(db.Integer, db.ForeignKey('movie_ratings.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    __table_args__ = (db.UniqueConstraint('user_id', 'rating_id', name='uq_user_rating_like'),)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# --- Routes ---

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    # Fetch personal ratings
    my_ratings = MovieRating.query.filter_by(user_id=session['user_id']).order_by(MovieRating.created_at.desc()).all()
    
    # NEW: Fetch Community Feed (all users)
    community_feed = MovieRating.query.order_by(MovieRating.created_at.desc()).limit(10).all()
    
    # NEW: Fetch Watchlist
    my_watchlist = Watchlist.query.filter_by(user_id=session['user_id']).order_by(Watchlist.added_at.desc()).all()

    top_movies = db.session.query(
        MovieRating.movie_id,
        MovieRating.movie_title,
        MovieRating.poster_url,
        func.round(func.avg(MovieRating.rating), 2).label('avg_rating'),
        func.count(MovieRating.id).label('votes')
    ).group_by(MovieRating.movie_id, MovieRating.movie_title, MovieRating.poster_url) \
     .having(func.count(MovieRating.id) >= 1) \
     .order_by(func.avg(MovieRating.rating).desc(), func.count(MovieRating.id).desc()) \
     .limit(10).all()

    recommendations = get_recommendations_for_user(session['user_id'], my_ratings)
    trending_movies = get_trending_movies()
    like_counts, liked_rating_ids = get_feed_like_meta(session['user_id'], community_feed)
    
    return render_template('index.html', 
                           recent_ratings=my_ratings, 
                           community_feed=community_feed, 
                           watchlist=my_watchlist,
                           top_movies=top_movies,
                           recommendations=recommendations,
                           trending_movies=trending_movies,
                           like_counts=like_counts,
                           liked_rating_ids=liked_rating_ids)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = hash_password(request.form['password'])
        user = User.query.filter_by(username=username, password=password).first()
        if user:
            session['user_id'] = user.id
            session['username'] = user.username
            session['is_admin'] = user.is_admin
            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'error')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = hash_password(request.form['password'])
        try:
            new_user = User(username=username, email=email, password=password)
            db.session.add(new_user)
            db.session.commit()
            flash('Registration successful!', 'success')
            return redirect(url_for('login'))
        except IntegrityError:
            db.session.rollback()
            flash('Username or email already exists', 'error')
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out', 'info')
    return redirect(url_for('login'))

# --- Movie Logic ---

@app.route('/search_movie', methods=['POST'])
def search_movie():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    movie_title = request.json.get('title', '').strip()
    if not movie_title:
        return jsonify({'error': 'Title required'}), 400

    try:
        movies = imdb.search_movie(movie_title)
        movie_data = None
        imdb_movie_id = None
        if movies:
            movie = movies[0]
            imdb_movie_id = movie.movieID
            imdb.update(movie, ['main'])
            movie_data = {
                'id': imdb_movie_id,
                'title': movie.get('title', 'N/A'),
                'year': movie.get('year', 'N/A'),
                'poster': movie.get('full-size cover url', ''),
                'plot': movie.get('plot outline', 'N/A'),
                'genres': movie.get('genres', [])
            }
        
        omdb_data = get_omdb_data(movie_title)
        if omdb_data:
            movie_data = merge_movie_data(movie_data or {}, omdb_data)
            if not movie_data.get('id'):
                movie_data['id'] = 'omdb_' + movie_title.replace(' ', '_').lower()

        return jsonify(movie_data) if movie_data else (jsonify({'error': 'Not found'}), 404)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_omdb_data(title):
    try:
        params = {'apikey': OMDB_API_KEY, 't': title, 'plot': 'short'}
        response = requests.get(OMDB_BASE_URL, params=params, timeout=5)
        data = response.json()
        if data.get('Response') == 'True':
            return {
                'title': data.get('Title'),
                'year': data.get('Year'),
                'rating': data.get('imdbRating'),
                'poster': data.get('Poster') if data.get('Poster') != 'N/A' else '',
                'plot': data.get('Plot'),
                'genres': [g.strip() for g in data.get('Genre', '').split(',') if g.strip()]
            }
    except: return None
    return None

def merge_movie_data(imdb_data, omdb_data):
    merged = imdb_data.copy()
    for key in ['title', 'year', 'poster', 'plot', 'rating']:
        if not merged.get(key) or merged.get(key) == 'N/A':
            merged[key] = omdb_data.get(key)
    if not merged.get('genres'):
        merged['genres'] = omdb_data.get('genres', [])
    return merged

@app.route('/search_suggestions')
def search_suggestions():
    if 'user_id' not in session:
        return jsonify([]), 401
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify([])
    try:
        suggestions = []
        movies = imdb.search_movie(query)[:6]
        for m in movies:
            suggestions.append({
                'title': m.get('title', ''),
                'year': m.get('year', 'N/A')
            })
        return jsonify(suggestions)
    except Exception:
        return jsonify([])

@app.route('/rate_movie', methods=['POST'])
def rate_movie():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.json
    movie_id = data.get('movie_id')
    movie_title = data.get('movie_title')
    poster_url = data.get('poster_url') # Captured from search
    rating = data.get('rating')
    review = data.get('review', '')
    genres = ','.join(data.get('genres', []))

    try:
        existing = MovieRating.query.filter_by(user_id=session['user_id'], movie_id=movie_id).first()
        if existing:
            existing.rating = rating
            existing.review = review
            existing.poster_url = poster_url
            existing.genres = genres
        else:
            new_rating = MovieRating(user_id=session['user_id'], movie_id=movie_id, 
                                     movie_title=movie_title, poster_url=poster_url,
                                     rating=rating, review=review, genres=genres)
            db.session.add(new_rating)
        
        # Remove from watchlist if it was there
        Watchlist.query.filter_by(user_id=session['user_id'], movie_id=movie_id).delete()
        
        db.session.commit()
        return jsonify({'success': 'Saved!'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/add_to_watchlist', methods=['POST'])
def add_to_watchlist():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    
    # Check if already in watchlist
    exists = Watchlist.query.filter_by(user_id=session['user_id'], movie_id=data.get('movie_id')).first()
    if not exists:
        item = Watchlist(user_id=session['user_id'], movie_id=data.get('movie_id'),
                         movie_title=data.get('movie_title'), poster_url=data.get('poster_url'))
        db.session.add(item)
        db.session.commit()
    return jsonify({'success': 'Added to watchlist'})

@app.route('/like_review/<int:rating_id>', methods=['POST'])
def like_review(rating_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    rating = MovieRating.query.get(rating_id)
    if not rating:
        return jsonify({'error': 'Review not found'}), 404
    if rating.user_id == session['user_id']:
        return jsonify({'error': 'Cannot like your own review'}), 400

    existing_like = ReviewLike.query.filter_by(user_id=session['user_id'], rating_id=rating_id).first()
    if existing_like:
        db.session.delete(existing_like)
        db.session.commit()
        liked = False
    else:
        db.session.add(ReviewLike(user_id=session['user_id'], rating_id=rating_id))
        db.session.commit()
        liked = True

    count = ReviewLike.query.filter_by(rating_id=rating_id).count()
    return jsonify({'success': True, 'liked': liked, 'count': count})

@app.route('/user/<username>')
def user_profile(username):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.filter_by(username=username).first_or_404()
    ratings = MovieRating.query.filter_by(user_id=user.id).order_by(MovieRating.created_at.desc()).all()
    watchlist = Watchlist.query.filter_by(user_id=user.id).order_by(Watchlist.added_at.desc()).all()
    return render_template('profile.html', profile_user=user, ratings=ratings, watchlist=watchlist)

# --- Admin Routes ---

@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    users = User.query.all()
    return render_template('admin.html', users=users, ratings_count=MovieRating.query.count())

@app.route('/admin/toggle_admin/<int:user_id>')
def toggle_admin(user_id):
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    # Prevent admins from accidentally removing their own admin role
    if user_id == session.get('user_id'):
        flash('You cannot change your own admin role.', 'error')
        return redirect(url_for('admin_panel'))

    user = User.query.get(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin_panel'))

    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f"Updated admin role for {user.username}.", 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/delete_user/<int:user_id>')
def delete_user(user_id):
    if not session.get('is_admin') or user_id == session['user_id']:
        return redirect(url_for('admin_panel'))
    MovieRating.query.filter_by(user_id=user_id).delete()
    Watchlist.query.filter_by(user_id=user_id).delete()
    user = User.query.get(user_id)
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('admin_panel'))

def get_feed_like_meta(current_user_id, feed):
    rating_ids = [r.id for r in feed]
    if not rating_ids:
        return {}, set()

    like_rows = db.session.query(
        ReviewLike.rating_id,
        func.count(ReviewLike.id).label('like_count')
    ).filter(ReviewLike.rating_id.in_(rating_ids)).group_by(ReviewLike.rating_id).all()
    like_counts = {row.rating_id: row.like_count for row in like_rows}

    liked_rows = ReviewLike.query.filter(
        ReviewLike.user_id == current_user_id,
        ReviewLike.rating_id.in_(rating_ids)
    ).all()
    liked_rating_ids = {row.rating_id for row in liked_rows}
    return like_counts, liked_rating_ids

def get_recommendations_for_user(user_id, my_ratings):
    high_ratings = [r for r in my_ratings if r.rating >= 8 and getattr(r, 'genres', None)]
    if not high_ratings:
        return []

    genre_counter = {}
    for r in high_ratings:
        genres = [g.strip() for g in (r.genres or '').split(',') if g.strip()]
        for g in genres:
            genre_counter[g] = genre_counter.get(g, 0) + 1

    top_genres = [genre for genre, _ in sorted(genre_counter.items(), key=lambda x: x[1], reverse=True)[:2]]
    if not top_genres:
        return []

    seen_movie_ids = {r.movie_id for r in my_ratings}
    recs = []
    for genre in top_genres:
        if len(recs) >= 8:
            break
        for movie in fetch_movies_by_genre(genre):
            movie_key = movie.get('id') or movie.get('title')
            if movie_key in seen_movie_ids:
                continue
            if movie_key in {m.get('id') or m.get('title') for m in recs}:
                continue
            recs.append(movie)
            if len(recs) >= 8:
                break
    return recs

def fetch_movies_by_genre(genre):
    # OMDb does not support direct "discover by genre", so this uses popular seed titles
    # and filters them by OMDb genre metadata.
    seed_titles = ['Inception', 'The Dark Knight', 'Interstellar', 'The Matrix', 'Parasite', 'The Godfather', 'Whiplash', 'Get Out', 'Mad Max: Fury Road', 'Dune']
    movies = []
    for title in seed_titles:
        data = get_omdb_data(title)
        if not data:
            continue
        if genre.lower() in [g.lower() for g in data.get('genres', [])]:
            movies.append({
                'id': f"omdb_{title.lower().replace(' ', '_')}",
                'title': data.get('title', title),
                'year': data.get('year', 'N/A'),
                'poster': data.get('poster', ''),
                'rating': data.get('rating', 'N/A'),
                'genres': data.get('genres', [])
            })
    return movies

def get_trending_movies():
    if TMDB_API_KEY:
        try:
            response = requests.get(
                'https://api.themoviedb.org/3/trending/movie/day',
                params={'api_key': TMDB_API_KEY},
                timeout=6
            )
            data = response.json()
            movies = []
            for item in data.get('results', [])[:10]:
                movies.append({
                    'title': item.get('title'),
                    'year': (item.get('release_date') or 'N/A')[:4],
                    'poster': f"https://image.tmdb.org/t/p/w342{item['poster_path']}" if item.get('poster_path') else '',
                    'rating': item.get('vote_average', 'N/A')
                })
            if movies:
                return movies
        except Exception:
            pass

    fallback_titles = ['Dune: Part Two', 'Oppenheimer', 'Barbie', 'Poor Things', 'The Batman', 'Everything Everywhere All at Once', 'Furiosa', 'Civil War', 'The Holdovers', 'Godzilla Minus One']
    trending = []
    for title in fallback_titles:
        data = get_omdb_data(title)
        if not data:
            continue
        trending.append({
            'title': data.get('title', title),
            'year': data.get('year', 'N/A'),
            'poster': data.get('poster', ''),
            'rating': data.get('rating', 'N/A')
        })
    return trending

db = SQLAlchemy(app)
migrate = Migrate(app, db)

with app.app_context():
    db.create_all()
    
if __name__ == '__main__':
    app.run(debug=True)
