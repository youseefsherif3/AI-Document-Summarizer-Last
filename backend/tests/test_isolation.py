"""Two users, A and B, each signed in through the real /auth/register (real cookies, real
CSRF check). Neither can list, open, search, chat with or delete the other's documents,
whatever IDs they send, and every route except the public ones requires a session."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.auth as auth
import app.database as database
import app.main as main
import app.services.chat as chat
from app.services.chunking import Chunk

pytestmark = pytest.mark.real_auth  # real users, no default signed-in user

ORIGIN = "http://localhost:3000"
DIMENSIONS = database.EMBEDDING_DIMENSIONS
PUBLIC_ROUTES = {
    ("GET", "/"),
    ("GET", "/health"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),  # works without a session: it only clears the cookie
}


def vec(*values):
    return list(values) + [0.0] * (DIMENSIONS - len(values))


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)


@pytest.fixture
def model(monkeypatch):
    """A fake chat model streaming "Answer" + " text"; records its requests."""
    fake = SimpleNamespace(requests=[])

    class Stream:
        def __aiter__(self):
            return self._chunks()

        async def _chunks(self):
            for part in ("Answer", " text"):
                await asyncio.sleep(0)
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=part))], usage=None
                )

        async def close(self):
            pass

    async def create(**kwargs):
        fake.requests.append(kwargs)
        return Stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(chat, "_get_async_client", lambda: client)
    return fake


class Browser:
    """One signed-in user with their own cookie jar."""

    def __init__(self, email: str):
        self.client = TestClient(main.app, base_url="https://testserver")
        self.client.__enter__()
        response = self.request("POST", "/auth/register",
                                json={"email": email, "password": "correct horse battery"})
        assert response.status_code == 201, response.text
        self.user_id = response.json()["id"]

    def request(self, method, path, **kwargs):
        headers = {"origin": ORIGIN, **kwargs.pop("headers", {})}
        return self.client.request(method, path, headers=headers, **kwargs)

    def documents(self) -> list[str]:
        response = self.request("GET", "/documents")
        assert response.status_code == 200
        return [document["filename"] for document in response.json()]

    def chat(self, message="Question?", **body):
        response = self.request("POST", "/chat/stream", json={"message": message, **body})
        return response, sse_events(response.text) if response.status_code == 200 else []

    def close(self):
        self.client.__exit__(None, None, None)


@pytest.fixture
def users():
    a, b = Browser("a@example.com"), Browser("b@example.com")
    yield a, b
    a.close()
    b.close()


def sse_events(body):
    return [json.loads(line[len("data: "):]) for frame in body.split("\n\n")
            for line in frame.split("\n") if line.startswith("data: ")]


def save(owner: int, filename: str, contents_and_vectors) -> int:
    chunks = [Chunk(index=i, page_number=i + 1, text=content, token_estimate=5,
                    start_char=0, end_char=len(content))
              for i, (content, _) in enumerate(contents_and_vectors)]
    return database.save_document_with_chunks(
        filename, ".txt", 10, b"x", "text", "A summary.", chunks,
        [vector for _, vector in contents_and_vectors], owner_user_id=owner,
    )


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


# --- Every route needs a session ----------------------------------------------------------


def all_routes() -> set[tuple[str, str]]:
    """Every API route, from the app's own OpenAPI schema (includes routers), so a route
    added later is checked automatically."""
    return {
        (method.upper(), path)
        for path, methods in main.app.openapi()["paths"].items()
        for method in methods
    }


def concrete(path: str) -> str:
    return path.replace("{document_id}", "1")


def test_the_public_routes_are_exactly_the_expected_ones():
    routes = all_routes()
    assert PUBLIC_ROUTES <= routes
    # If this fails, a route was added: decide whether it is public, and update the list.
    assert routes - PUBLIC_ROUTES == {
        ("GET", "/auth/me"),
        ("GET", "/documents"),
        ("GET", "/dashboard"),
        ("POST", "/documents/upload/stream"),
        ("POST", "/documents/upload/stream/blob"),
        ("POST", "/chat/stream"),
        ("DELETE", "/documents/{document_id}"),
    }


def test_every_other_route_returns_401_without_a_session(model):
    with TestClient(main.app, base_url="https://testserver") as client:
        for method, path in sorted(all_routes() - PUBLIC_ROUTES):
            response = client.request(method, concrete(path), headers={"origin": ORIGIN})
            assert response.status_code == 401, (method, path, response.text)
            assert response.json() == {"detail": "Not signed in."}
    assert model.requests == []


def test_every_route_that_changes_something_needs_an_allowed_origin(users, model):
    a, _ = users
    save(a.user_id, "a.txt", [("A's text.", vec(1))])
    unsafe = [(m, p) for m, p in all_routes() if m in ("POST", "DELETE")]
    for method, path in unsafe:
        for origin in ("https://evil.example.com", None):
            headers = {"origin": origin} if origin else {}
            response = a.client.request(method, concrete(path), headers=headers)
            assert response.status_code == 403, (method, path, origin, response.text)
    assert a.documents() == ["a.txt"]  # the forged delete did nothing
    assert model.requests == []


# --- Documents ----------------------------------------------------------------------------


def test_each_user_lists_only_their_own_documents(users):
    a, b = users
    save(a.user_id, "a-plan.txt", [("A's plan.", vec(1))])
    save(a.user_id, "a-notes.txt", [("A's notes.", vec(1))])
    save(b.user_id, "b-secret.txt", [("B's secret.", vec(1))])
    # A document without an owner (uploaded before accounts) is nobody's.
    sql("INSERT INTO documents (filename, file_type, file_size, file_content, extracted_text) "
        "VALUES ('unclaimed.txt', '.txt', 1, 'x', 'x')")

    assert sorted(a.documents()) == ["a-notes.txt", "a-plan.txt"]
    assert b.documents() == ["b-secret.txt"]


def test_the_dashboard_counts_only_the_users_documents(users):
    a, b = users
    save(a.user_id, "a.txt", [("A.", vec(1))])
    save(b.user_id, "b1.pdf", [("B.", vec(1))])
    save(b.user_id, "b2.pdf", [("B.", vec(1))])

    a_dashboard = a.request("GET", "/dashboard").json()
    b_dashboard = b.request("GET", "/dashboard").json()

    assert a_dashboard["total_documents"] == 1
    assert [d["filename"] for d in a_dashboard["recent_documents"]] == ["a.txt"]
    assert a_dashboard["file_types"] == {"txt": 1}
    assert b_dashboard["total_documents"] == 2
    assert {d["filename"] for d in b_dashboard["recent_documents"]} == {"b1.pdf", "b2.pdf"}


def test_a_user_cannot_delete_another_users_document(users):
    a, b = users
    bs = save(b.user_id, "b.txt", [("B's text.", vec(1))])

    response = a.request("DELETE", f"/documents/{bs}")

    # The same answer as for a document that doesn't exist.
    assert response.status_code == 404
    assert response.json() == {"detail": "Document not found."}
    assert a.request("DELETE", "/documents/999999").json() == {"detail": "Document not found."}
    assert b.documents() == ["b.txt"]
    assert sql("SELECT count(*) FROM chunks WHERE document_id = :id", {"id": bs}).scalar_one() == 1
    # The owner can.
    assert b.request("DELETE", f"/documents/{bs}").status_code == 200
    assert b.documents() == []


def test_uploads_belong_to_the_uploader(users, monkeypatch):
    a, b = users

    async def summary(text):
        yield "A summary."

    monkeypatch.setattr(main, "stream_summary", summary)

    response = a.request("POST", "/documents/upload/stream",
                         files={"file": ("mine.txt", b"A's uploaded text.", "text/plain")})

    assert response.status_code == 200
    done = [e for e in sse_events(response.text) if e["type"] == "done"][0]
    owner = sql("SELECT owner_user_id FROM documents WHERE id = :id",
                {"id": done["document_id"]}).scalar_one()
    assert owner == a.user_id
    assert a.documents() == ["mine.txt"]
    assert b.documents() == []


def test_blob_uploads_belong_to_the_uploader(users, monkeypatch):
    a, b = users
    content = b"B's blob text."

    async def head(pathname):
        return SimpleNamespace(size=len(content))

    async def get(pathname, *, access):
        return SimpleNamespace(body=content)

    async def delete(pathname):
        pass

    async def summary(text):
        yield "A summary."

    monkeypatch.setattr(main.blob, "head_blob", head)
    monkeypatch.setattr(main.blob, "get_blob", get)
    monkeypatch.setattr(main.blob, "delete_blob", delete)
    monkeypatch.setattr(main, "stream_summary", summary)

    response = b.request("POST", "/documents/upload/stream/blob", json={
        "pathname": "documind/uploads/b-random.txt", "filename": "b-blob.txt",
        "file_size": len(content),
    })

    assert response.status_code == 200
    assert b.documents() == ["b-blob.txt"]
    assert a.documents() == []


# --- Chat ---------------------------------------------------------------------------------


def test_chat_with_another_users_document_ids_is_404(users, model):
    a, b = users
    mine = save(a.user_id, "a.txt", [("A's text.", vec(1))])
    theirs = save(b.user_id, "b.txt", [("B's text.", vec(1))])

    for document_ids in ([theirs], [mine, theirs], [999999]):
        response, _ = a.chat(document_ids=document_ids)
        assert response.status_code == 404, document_ids
        assert response.json() == {"detail": "Document not found."}
    assert model.requests == []
    # With only their own document, A gets an answer.
    response, events = a.chat(document_ids=[mine])
    assert response.status_code == 200
    assert {s["document_id"] for s in events[0]["sources"]} == {mine}


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
def test_chat_search_never_returns_another_users_chunks(users, model, monkeypatch, mode):
    """B's chunk is the best match both ways: its vector equals the question's and it has the
    question's rare word. A must still get only A's chunks, from both searches."""
    a, b = users
    monkeypatch.setenv("RETRIEVAL_MODE", mode)
    # The conftest's fake question embedding is vec(1).
    theirs = save(b.user_id, "b-zebracorn.txt", [("The Zebracorn budget is 9,999 EUR.", vec(1))])
    mine = save(a.user_id, "a-notes.txt", [("Notes about something else.", vec(0.2, 0.98))])

    response, events = a.chat("What is the Zebracorn budget?")

    assert response.status_code == 200
    sources = events[0]["sources"]
    assert [s["document_id"] for s in sources] == [mine]
    assert all("Zebracorn" not in s["snippet"] for s in sources)
    assert "Zebracorn budget is 9,999" not in json.dumps(model.requests)
    # B, asking the same question, does get B's chunk.
    _, b_events = b.chat("What is the Zebracorn budget?")
    assert [s["document_id"] for s in b_events[0]["sources"]] == [theirs]


def test_both_searches_filter_by_owner_at_the_database(users):
    a, b = users
    for n in range(30):  # many of B's chunks, all closer to the query than A's
        save(b.user_id, f"b{n}.txt", [(f"Zebracorn report {n}.", vec(1))])
    for n in range(10):
        save(a.user_id, f"a{n}.txt", [(f"Zebracorn note {n}.", vec(0.5, 0.5))])

    vector = database.search_chunks(vec(1), k=8, owner_user_id=a.user_id)
    words = database.full_text_search_chunks(vec(1), "zebracorn", k=20, owner_user_id=a.user_id)

    # A still gets a full top 8 of A's own chunks, though B's 30 are all closer.
    assert len(vector) == 8
    assert {m.filename[0] for m in vector} == {"a"}
    assert len(words) == 10 and {m.filename[0] for m in words} == {"a"}


def test_a_users_chat_streams_exactly_as_before(users, model):
    a, _ = users
    mine = save(a.user_id, "a.txt", [("A's text.", vec(1))])

    response, events = a.chat()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [e["type"] for e in events] == ["sources", "delta", "delta", "done"]
    assert [e["content"] for e in events if e["type"] == "delta"] == ["Answer", " text"]
    assert events[0]["sources"][0]["document_id"] == mine


def test_a_renewed_session_cookie_arrives_with_the_chat_stream(users, model, monkeypatch):
    a, _ = users
    save(a.user_id, "a.txt", [("A's text.", vec(1))])
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    monkeypatch.setattr(auth, "_now", lambda: later)

    response, events = a.chat()

    assert [e["type"] for e in events] == ["sources", "delta", "delta", "done"]
    assert "Max-Age=" in response.headers.get("set-cookie", "")
    assert "HttpOnly" in response.headers["set-cookie"]


def test_after_logout_nothing_is_reachable(users, model):
    a, _ = users
    save(a.user_id, "a.txt", [("A's text.", vec(1))])
    assert a.request("POST", "/auth/logout").status_code == 204

    assert a.request("GET", "/documents").status_code == 401
    assert a.chat()[0].status_code == 401
    assert model.requests == []
