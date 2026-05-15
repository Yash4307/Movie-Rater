# MovieRater

MovieRater is a Flask + MySQL web app where users can search movies, rate/review them, save watchlist items, discover community activity, and (for admins) manage users.

## Features
- User registration/login/logout with role-aware navigation
- Movie search using IMDb + OMDb metadata
- 5-star review flow (stored as 10-point ratings)
- Personal ratings gallery with poster support
- Watchlist ("Watch Later")
- Community feed
- Review likes/hearts
- User profiles: `/user/<username>`
- Admin dashboard (user management + stats)
- Glassmorphism-style responsive UI

## Tech Stack
- Backend: Python, Flask, Flask-SQLAlchemy
- Database: MySQL
- Optional migrations: Flask-Migrate
- Frontend: HTML, CSS, JavaScript, Font Awesome
- External APIs: IMDbPY, OMDb, optional TMDB trending

## Project Structure
```text
movie_rating_app/
|- app.py
|- requirements.txt
|- templates/
|- static/
|- MIGRATIONS.md
|- README.md
```

## Environment Variables
Create a `.env` file in project root:

```env
SECRET_KEY=your_secret_key
OMDB_API_KEY=your_omdb_api_key

# Preferred single connection string
DATABASE_URL=mysql+pymysql://user:password@host:3306/db_name

# Optional fallback (used if DATABASE_URL is not set)
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=localhost
DB_NAME=your_db_name

# Optional (enables TMDB trending endpoint)
TMDB_API_KEY=your_tmdb_api_key
```

## Local Setup (GitHub / Dev Machine)
1. Clone repository:
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
```

2. Create and activate virtual environment:
```bash
python -m venv myenv
# Windows PowerShell
myenv\Scripts\Activate.ps1
# macOS/Linux
source myenv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Prepare database schema.

### Option A: Manual MySQL (Workbench)
Run these SQL commands in your app database:

```sql
ALTER TABLE movie_ratings
ADD COLUMN IF NOT EXISTS genres VARCHAR(500) NULL AFTER poster_url;

CREATE TABLE IF NOT EXISTS review_likes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    rating_id INT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_user_rating_like UNIQUE (user_id, rating_id),
    CONSTRAINT fk_review_likes_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_review_likes_rating
        FOREIGN KEY (rating_id) REFERENCES movie_ratings(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_review_likes_user_id ON review_likes(user_id);
CREATE INDEX idx_review_likes_rating_id ON review_likes(rating_id);
```

### Option B: Flask-Migrate (recommended long-term)
```bash
flask db init         # only if migrations/ does not exist
flask db migrate -m "add review likes and rating genres"
flask db upgrade
```

Important: Use either manual SQL or migrations consistently to avoid schema drift.

5. Run app:
```bash
python app.py
```
Open: `http://localhost:5001`

## Render Deployment (Flask + MySQL)

### 1. Push project to GitHub
Render will deploy directly from your GitHub repo.

### 2. Create MySQL database
Use either:
- Render managed MySQL (if available in your plan/region), or
- External MySQL provider (Railway, PlanetScale, Aiven, etc.)

Copy the full MySQL connection URL.

### 3. Create a new Web Service on Render
- Runtime: `Python`
- Build Command:
```bash
pip install -r requirements.txt
```
- Start Command:
```bash
gunicorn app:app --bind 0.0.0.0:$PORT
```

If `gunicorn` is missing, add it to `requirements.txt`.

### 4. Add Render Environment Variables
Set these in Render dashboard:
- `SECRET_KEY`
- `OMDB_API_KEY`
- `DATABASE_URL` (MySQL URL)
- `TMDB_API_KEY` (optional)

### 5. Database migrations on Render
If using Flask-Migrate, run one-off command in Render shell:
```bash
flask db upgrade
```

If using manual SQL, run schema SQL in MySQL Workbench before first deploy.

### 6. Redeploy
Trigger deploy from Render dashboard or by pushing a new commit.

## Notes
- `SQLALCHEMY_ENGINE_OPTIONS` includes `pool_pre_ping=True` to reduce stale MySQL connection issues.
- Keep `.env` out of GitHub (`.gitignore`).
- Do not hardcode secrets in `app.py`.

## Troubleshooting
- `ModuleNotFoundError: flask_migrate`:
```bash
pip install Flask-Migrate
```
- Confirm active interpreter:
```bash
where python
where pip
```
Both should point to your virtual environment.

## License
Add your preferred license here (MIT, Apache-2.0, etc.).
