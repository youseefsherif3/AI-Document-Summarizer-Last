"""`python -m app.claim_documents`: gives documents without an owner to one account."""

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.auth as auth
import app.database as database
from app import claim_documents
from app.main import app


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


def add_user(email: str) -> int:
    return sql("INSERT INTO users (email, password_hash) VALUES (:email, 'hash') RETURNING id",
               {"email": email}).scalar_one()


def add_document(filename: str, owner: int | None = None) -> int:
    document_id = database.save_document_with_chunks(
        filename=filename, file_type=".txt", file_size=4, file_content=b"text",
        extracted_text="text", summary="A summary.", document_chunks=[], embeddings=[],
    )
    if owner is not None:
        sql("UPDATE documents SET owner_user_id = :owner WHERE id = :id",
            {"owner": owner, "id": document_id})
    return document_id


def owners() -> dict[str, int | None]:
    return dict(sql("SELECT filename, owner_user_id FROM documents").all())


@pytest.fixture
def documents():
    """Two documents without an owner and one that Ben already owns."""
    ana = add_user("ana@example.com")
    ben = add_user("ben@example.com")
    add_document("old-1.txt")
    add_document("old-2.txt")
    add_document("bens.txt", owner=ben)
    return {"ana": ana, "ben": ben}


def test_dry_run_lists_what_would_be_claimed_and_changes_nothing(documents, capsys):
    before = owners()

    assert claim_documents.main(["--email", "ana@example.com", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "Dry run: 2 document(s) have no owner and would be claimed" in out
    assert f"by account {documents['ana']}" in out
    assert "old-1.txt" in out and "old-2.txt" in out
    assert "bens.txt" not in out
    assert "Nothing was changed." in out
    assert owners() == before


def test_claiming_assigns_only_documents_without_an_owner(documents, capsys):
    assert claim_documents.main(["--email", "ana@example.com"]) == 0

    assert "Claimed 2 document(s)" in capsys.readouterr().out
    assert owners() == {
        "old-1.txt": documents["ana"],
        "old-2.txt": documents["ana"],
        "bens.txt": documents["ben"],  # untouched
    }


def test_running_it_again_claims_nothing(documents, capsys):
    claim_documents.main(["--email", "ana@example.com"])
    capsys.readouterr()

    assert claim_documents.main(["--email", "ana@example.com"]) == 0
    assert "Claimed 0 document(s)" in capsys.readouterr().out
    # Another account can't take them over afterwards either.
    assert claim_documents.main(["--email", "ben@example.com"]) == 0
    assert set(owners().values()) == {documents["ana"], documents["ben"]}


def test_the_email_is_normalized(documents):
    assert claim_documents.main(["--email", "  Ana@Example.COM "]) == 0
    assert owners()["old-1.txt"] == documents["ana"]


def test_an_unknown_email_changes_nothing(documents, capsys):
    before = owners()

    assert claim_documents.main(["--email", "nobody@example.com"]) == 1

    err = capsys.readouterr().err
    assert "No account with the email 'nobody@example.com'" in err
    assert "nothing was changed" in err
    assert owners() == before


def test_it_refuses_a_database_that_is_not_migrated(monkeypatch, capsys):
    def not_ready():
        raise RuntimeError("The database has no migration history.")

    monkeypatch.setattr(database, "check_database", not_ready)
    assert claim_documents.main(["--email", "ana@example.com"]) == 1
    assert "The database is not ready" in capsys.readouterr().err


def test_registering_never_claims_documents(monkeypatch):
    add_document("old.txt")
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3000")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/auth/register",
            json={"email": "ana@example.com", "password": "correct horse battery"},
            headers={"origin": "http://localhost:3000"},
        )

    assert response.status_code == 201
    assert owners() == {"old.txt": None}


def test_deleting_an_account_deletes_its_documents_and_their_chunks(documents):
    claim_documents.main(["--email", "ana@example.com"])
    sql("INSERT INTO chunks (document_id, chunk_index, content, token_estimate, embedding) "
        "SELECT id, 0, 'chunk', 1, CAST(:vector AS vector) FROM documents",
        {"vector": "[" + ",".join(["0"] * (database.EMBEDDING_DIMENSIONS - 1) + ["1"]) + "]"})

    sql("DELETE FROM users WHERE id = :id", {"id": documents["ana"]})

    assert owners() == {"bens.txt": documents["ben"]}
    assert sql("SELECT count(*) FROM chunks").scalar_one() == 1
