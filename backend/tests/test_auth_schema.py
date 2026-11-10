"""The account tables from migration 0002_auth_tables: their constraints, checked against
the test database (built from the migrations)."""

import hashlib
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.database as database


def run(sql: str, params: dict | None = None):
    with database.get_connection() as connection:
        return connection.execute(text(sql), params or {})


def add_user(email: str = "ana@example.com") -> int:
    return run(
        "INSERT INTO users (email, password_hash) VALUES (:email, 'argon2-hash') RETURNING id",
        {"email": email},
    ).scalar_one()


def add_session(user_id: int, token: str = "token") -> None:
    run(
        "INSERT INTO sessions (user_id, token_hash, expires_at) "
        "VALUES (:user_id, :hash, now() + interval '14 days')",
        {"user_id": user_id, "hash": hashlib.sha256(token.encode()).digest()},
    )


def count(table: str) -> int:
    return run(f"SELECT count(*) FROM {table}").scalar_one()


def test_an_email_can_have_only_one_account():
    add_user("ana@example.com")
    with pytest.raises(IntegrityError, match="uq_users_email"):
        add_user("ana@example.com")


@pytest.mark.parametrize("email", ["Ana@example.com", " ana@example.com", "ana@example.com ", ""])
def test_emails_must_be_stored_normalized(email):
    # So "Ana@x.com" and "ana@x.com" can never be two accounts.
    with pytest.raises(IntegrityError, match="ck_users_email_normalized"):
        add_user(email)


def test_new_rows_get_their_timestamps_from_the_database():
    user_id = add_user()
    add_session(user_id)
    row = run(
        "SELECT u.created_at AS user_created, s.created_at, s.last_used_at, s.revoked_at "
        "FROM users u JOIN sessions s ON s.user_id = u.id"
    ).mappings().one()
    assert row["user_created"] is not None and row["user_created"].tzinfo is not None
    assert row["created_at"] is not None and row["last_used_at"] is not None
    assert row["revoked_at"] is None


def test_a_session_token_hash_is_unique_and_exactly_32_bytes():
    user_id = add_user()
    add_session(user_id, "same token")
    with pytest.raises(IntegrityError, match="uq_sessions_token_hash"):
        add_session(user_id, "same token")
    with pytest.raises(IntegrityError, match="ck_sessions_token_hash_sha256"):
        run(
            "INSERT INTO sessions (user_id, token_hash, expires_at) "
            "VALUES (:user_id, :hash, now())",
            {"user_id": user_id, "hash": b"not a sha256"},
        )


def test_a_session_needs_an_existing_user():
    with pytest.raises(IntegrityError, match="sessions_user_id_fkey"):
        add_session(999)


def test_deleting_a_user_deletes_their_sessions_and_usage_only():
    ana = add_user("ana@example.com")
    ben = add_user("ben@example.com")
    add_session(ana, "ana token")
    add_session(ben, "ben token")
    for user_id in (ana, ben):
        run("INSERT INTO daily_usage (user_id, day, uploads) VALUES (:id, :day, 1)",
            {"id": user_id, "day": date(2026, 11, 10)})

    run("DELETE FROM users WHERE id = :id", {"id": ana})

    assert run("SELECT user_id FROM sessions").scalars().all() == [ben]
    assert run("SELECT user_id FROM daily_usage").scalars().all() == [ben]


def test_daily_usage_has_one_row_per_user_and_day_and_never_goes_negative():
    user_id = add_user()
    run("INSERT INTO daily_usage (user_id, day) VALUES (:id, :day)",
        {"id": user_id, "day": date(2026, 11, 10)})
    row = run("SELECT uploads, chat_questions FROM daily_usage").one()
    assert tuple(row) == (0, 0)

    with pytest.raises(IntegrityError, match="daily_usage_pkey"):
        run("INSERT INTO daily_usage (user_id, day) VALUES (:id, :day)",
            {"id": user_id, "day": date(2026, 11, 10)})
    with pytest.raises(IntegrityError, match="ck_daily_usage_not_negative"):
        run("UPDATE daily_usage SET uploads = -1")

    # Another day is another row.
    run("INSERT INTO daily_usage (user_id, day) VALUES (:id, :day)",
        {"id": user_id, "day": date(2026, 11, 11)})
    assert count("daily_usage") == 2


def test_login_attempts_accept_only_login_and_register():
    for kind in ("login", "register"):
        run("INSERT INTO login_attempts (kind, email, ip, succeeded) "
            "VALUES (:kind, 'ana@example.com', '203.0.113.7', false)", {"kind": kind})
    with pytest.raises(IntegrityError, match="ck_login_attempts_kind"):
        run("INSERT INTO login_attempts (kind, succeeded) VALUES ('reset', false)")
    assert count("login_attempts") == 2


def test_rate_limit_lookups_use_their_indexes():
    indexes = set(run(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'login_attempts'"
    ).scalars())
    assert any("(email, created_at)" in index for index in indexes)
    assert any("(ip, created_at)" in index for index in indexes)
