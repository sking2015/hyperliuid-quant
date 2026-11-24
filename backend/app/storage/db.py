from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL, ROOT


@contextmanager
def connect():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        yield conn


def migrate():
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(6022026)")
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)")
        applied = {row["name"] for row in db.execute("SELECT name FROM schema_migrations")}
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            if path.name not in applied:
                db.execute(path.read_text(encoding="utf-8"))
                db.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))


if __name__ == "__main__":
    migrate()

