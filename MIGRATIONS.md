# Database Migration Notes

After pulling these changes, run migrations instead of `db.create_all()` only:

```bash
flask db init
flask db migrate -m "add review likes and rating genres"
flask db upgrade
```

If `migrations/` already exists, skip `flask db init`.

New schema elements:
- `movie_ratings.genres` (String)
- `review_likes` table with unique (`user_id`, `rating_id`)
