"""Create signed-in load-test users in the local `docsum_load` database (no sign-in calls).

    python loadtest/seed_users.py --users 100     create users and sessions, write users.json
    python loadtest/seed_users.py --clean         delete them (and their documents, jobs, usage)

Each user gets a session whose token is written to loadtest/users.json (not committed); the load
test sends it as the session cookie. That avoids the sign-in and registration rate limits, which
exist to stop abuse and would stop the test. The script refuses to touch any database that is not
a local `*_load` database, so it can never seed production or the dev database.
Run it from backend/'s virtualenv: backend/.venv/Scripts/python loadtest/seed_users.py ...
"""

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.database import sqlalchemy_url  # noqa: E402
from app.migrate import upgrade  # noqa: E402

LOAD_DATABASE_URL = "postgresql://docsum:docsum@127.0.0.1:5432/docsum_load"
EMAIL_DOMAIN = "loadtest.invalid"
USERS_FILE = Path(__file__).with_name("users.json")


def check_url(url: str) -> None:
    parsed = make_url(url)
    if parsed.host not in ("127.0.0.1", "localhost") or not (parsed.database or "").endswith("_load"):
        raise SystemExit(f"Refusing to use {parsed.host}/{parsed.database}: only a local *_load database.")


def ensure_database(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(sqlalchemy_url(str(parsed.set(database="postgres").render_as_string(False))),
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                                    {"n": parsed.database}).scalar()
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
            print(f"Created database {parsed.database}.")
    admin.dispose()


def seed(url: str, count: int) -> None:
    ensure_database(url)
    upgrade(url)  # the API container refuses to start on an unmigrated database
    engine = create_engine(sqlalchemy_url(url))
    users = []
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE email LIKE :p"), {"p": f"%@{EMAIL_DOMAIN}"})
        for i in range(count):
            email = f"load-{i}@{EMAIL_DOMAIN}"
            user_id = connection.execute(
                text("INSERT INTO users (email, password_hash) VALUES (:e, 'not-a-login') RETURNING id"),
                {"e": email},
            ).scalar_one()
            token = secrets.token_urlsafe(32)
            connection.execute(
                text("INSERT INTO sessions (user_id, token_hash, expires_at) "
                     "VALUES (:u, :h, now() + interval '1 day')"),
                {"u": user_id, "h": hashlib.sha256(token.encode()).digest()},
            )
            users.append({"id": user_id, "email": email, "token": token})
    engine.dispose()
    USERS_FILE.write_text(json.dumps({"users": users}, indent=1), encoding="utf-8")
    print(f"Seeded {count} users in {make_url(url).database}; tokens in {USERS_FILE.name}.")


def clean(url: str) -> None:
    engine = create_engine(sqlalchemy_url(url))
    with engine.begin() as connection:
        deleted = connection.execute(text("DELETE FROM users WHERE email LIKE :p"),
                                     {"p": f"%@{EMAIL_DOMAIN}"}).rowcount
    engine.dispose()
    USERS_FILE.unlink(missing_ok=True)
    print(f"Deleted {deleted} load-test users.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--database-url", default=LOAD_DATABASE_URL)
    args = parser.parse_args()
    check_url(args.database_url)
    clean(args.database_url) if args.clean else seed(args.database_url, args.users)
