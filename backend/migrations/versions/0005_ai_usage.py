"""One row per AI call, for usage dashboards.

`ai_usage` records every summary, chat, embedding and rewrite call made for a user: kind,
model, tokens, cost, duration, outcome. Rows belong to the user (deleting the account deletes
them) and are only ever read for that user, or summed system-wide for /metrics. `job_id` is the
upload job the call belonged to, if any (no foreign key: it is only a label).

Revision ID: 0005_ai_usage
Revises: 0004_processing_jobs
Create Date: 2026-11-11
"""

from alembic import op

revision = "0005_ai_usage"
down_revision = "0004_processing_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_usage (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            job_id UUID,
            kind TEXT NOT NULL,
            model TEXT NOT NULL,
            outcome TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd NUMERIC(14, 8),
            cost_source TEXT,
            duration_ms INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # A user's recent calls, newest first (the future usage page).
    op.execute(
        "CREATE INDEX ix_ai_usage_user_created "
        "ON ai_usage USING btree (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE ai_usage")
