# Database migrations

The schema is managed by Alembic (through Flask-Migrate). The `migrations/` folder holds the history, starting from one baseline revision that creates every table.

## What happens at startup

`init_schema()` in `app.py` runs on every start:

- **Empty database:** all migrations run, creating every table.
- **Database created before migrations existed:** missing tables are added, then it is marked as being at the baseline revision.
- **Up to date:** nothing happens.

This is why Render's free plan, which has no shell, still gets schema changes: they apply when the new version starts. Run a single web worker (the default in the start command) so two processes never migrate at once.

## Changing the schema

1. Edit `models.py`.
2. Generate a migration against a database that is up to date (for local work, run the app once first):
   ```powershell
   flask --app app db migrate -m "describe the change"
   ```
3. Read the new file in `migrations/versions/` and fix anything autogenerate got wrong. It cannot detect renames, and SQLite cannot alter some constraints.
4. Apply it locally by starting the app, run the tests, commit the file, deploy.

Never edit a migration that has already been applied to a real database; add a new one instead.

## Notes

- `SKIP_SCHEMA_INIT=1` skips the automatic upgrade (only needed when working on migrations themselves).
- The old `b2a8a1d299de` migration from the first version of this project was removed: the baseline replaces it, and keeping both makes Alembic report multiple heads.
