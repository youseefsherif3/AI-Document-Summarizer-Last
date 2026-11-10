"""Accounts: users, sessions, login attempts and daily usage counters.

- users: one row per account. The email is stored normalized (trimmed, lowercased), and
  the database refuses anything else, so "A@x.com" and "a@x.com" can't be two accounts.
- sessions: the browser's cookie holds a random token; only its SHA-256 (32 bytes) is
  stored. Expiry slides forward on use; logout sets revoked_at.
- login_attempts: failed and successful logins and registrations, for rate limiting by
  email and by client IP. Old rows are deleted by the app.
- daily_usage: uploads and chat questions per user per UTC day, for the daily limits.

Deleting a user deletes their sessions and usage rows.

Revision ID: 0002_auth_tables
Revises: 0001_baseline
Create Date: 2026-11-10
"""

from alembic import op

revision = "0002_auth_tables"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id SERIAL NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT users_pkey PRIMARY KEY (id),
            CONSTRAINT uq_users_email UNIQUE (email),
            CONSTRAINT ck_users_email_normalized
                CHECK (email = lower(btrim(email)) AND email <> '')
        )
        """
    )

    op.execute(
        """
        CREATE TABLE sessions (
            id SERIAL NOT NULL,
            user_id INTEGER NOT NULL,
            token_hash BYTEA NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            last_used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            revoked_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT sessions_pkey PRIMARY KEY (id),
            CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash),
            CONSTRAINT ck_sessions_token_hash_sha256 CHECK (octet_length(token_hash) = 32)
        )
        """
    )
    op.execute("CREATE INDEX ix_sessions_user_id ON sessions USING btree (user_id)")

    op.execute(
        """
        CREATE TABLE login_attempts (
            id BIGSERIAL NOT NULL,
            kind TEXT NOT NULL,
            email TEXT,
            ip TEXT,
            succeeded BOOLEAN NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT login_attempts_pkey PRIMARY KEY (id),
            CONSTRAINT ck_login_attempts_kind CHECK (kind IN ('login', 'register'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_login_attempts_email_created "
        "ON login_attempts USING btree (email, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_login_attempts_ip_created "
        "ON login_attempts USING btree (ip, created_at)"
    )

    op.execute(
        """
        CREATE TABLE daily_usage (
            user_id INTEGER NOT NULL,
            day DATE NOT NULL,
            uploads INTEGER NOT NULL DEFAULT 0,
            chat_questions INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT daily_usage_pkey PRIMARY KEY (user_id, day),
            CONSTRAINT daily_usage_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_daily_usage_not_negative CHECK (uploads >= 0 AND chat_questions >= 0)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE daily_usage")
    op.execute("DROP TABLE login_attempts")
    op.execute("DROP TABLE sessions")
    op.execute("DROP TABLE users")
