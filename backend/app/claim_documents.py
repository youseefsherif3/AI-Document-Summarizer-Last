"""Give documents that have no owner to one account. Run by hand, once, after registering.

Documents uploaded before accounts existed have no owner, so nobody can see them. This
command assigns all of them to the account with the given email:

    python -m app.claim_documents --email you@example.com --dry-run   # shows what it would do
    python -m app.claim_documents --email you@example.com             # does it

It uses DATABASE_URL (the same database as the app). Only documents with no owner are
touched, so running it again claims nothing. Exit code 0 means success (also when there is
nothing to claim), 1 means the account doesn't exist or the database isn't ready.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app import database
from app.auth import normalize_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnownedDocument:
    id: int
    filename: str
    created_at: datetime | None


def find_user_id(email: str) -> int | None:
    with database.get_connection() as connection:
        return connection.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": normalize_email(email)}
        ).scalar_one_or_none()


def unowned_documents() -> list[UnownedDocument]:
    with database.get_connection() as connection:
        rows = connection.execute(
            text("SELECT id, filename, created_at FROM documents "
                 "WHERE owner_user_id IS NULL ORDER BY created_at, id")
        ).all()
    return [UnownedDocument(*row) for row in rows]


def claim_unowned_documents(user_id: int) -> list[int]:
    """Assign every document without an owner to the user, in one transaction."""
    with database.get_connection() as connection:
        return connection.execute(
            text("UPDATE documents SET owner_user_id = :user_id "
                 "WHERE owner_user_id IS NULL RETURNING id"),
            {"user_id": user_id},
        ).scalars().all()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Give every document that has no owner to one account."
    )
    parser.add_argument("--email", required=True, help="the account's email (already registered)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only show which documents would be claimed")
    args = parser.parse_args(argv)

    try:
        database.check_database()
    except RuntimeError as error:
        print(f"The database is not ready: {error}", file=sys.stderr)
        return 1

    user_id = find_user_id(args.email)
    if user_id is None:
        print(f"No account with the email {normalize_email(args.email)!r}. Register first; "
              "nothing was changed.", file=sys.stderr)
        return 1

    documents = unowned_documents()
    if args.dry_run:
        print(f"Dry run: {len(documents)} document(s) have no owner and would be claimed "
              f"by account {user_id}. Nothing was changed.")
        for document in documents:
            print(f"  {document.id}\t{document.created_at}\t{document.filename}")
        return 0

    claimed = claim_unowned_documents(user_id)
    logger.info("Claimed %s document(s) without an owner for account %s.", len(claimed), user_id)
    print(f"Claimed {len(claimed)} document(s) for account {user_id}.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("alembic").setLevel(logging.WARNING)  # its plugin setup is noise here
    raise SystemExit(main())
