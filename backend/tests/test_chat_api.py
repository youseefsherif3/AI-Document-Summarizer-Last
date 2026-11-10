import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

import app.database as database
from tests.default_user import DEFAULT_USER_EMAIL, DEFAULT_USER_ID
from app.auth import User
import app.main as main
import app.services.chat as chat
from app.services.chunking import Chunk

DIMENSIONS = database.EMBEDDING_DIMENSIONS
REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def vec(*values):
    return list(values) + [0.0] * (DIMENSIONS - len(values))


def save_document(filename, contents, pages=None, vectors=None):
    chunks = [
        Chunk(index=i, page_number=(pages[i] if pages else i + 1), text=content,
              token_estimate=5, start_char=0, end_char=len(content))
        for i, content in enumerate(contents)
    ]
    vectors = vectors or [vec(1) for _ in contents]
    return database.save_document_with_chunks(
        filename, ".pdf", 10, b"x", "text", "summary", chunks, vectors,
        owner_user_id=DEFAULT_USER_ID,
    )


def sse_events(body):
    return [
        json.loads(line[len("data: "):])
        for frame in body.split("\n\n")
        for line in frame.split("\n")
        if line.startswith("data: ")
    ]


def chunk(content, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))], usage=usage
    )


class FakeStream:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for item in self.chunks:
            await asyncio.sleep(0)
            yield item
        if self.error:
            raise self.error

    async def close(self):
        self.closed = True


@pytest.fixture
def model(monkeypatch):
    """A fake chat model. Set .stream before the request; .requests records each call."""
    fake = SimpleNamespace(requests=[], stream=None)

    async def create(**kwargs):
        fake.requests.append(kwargs)
        return fake.stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(chat, "_get_async_client", lambda: client)
    return fake


USAGE = SimpleNamespace(prompt_tokens=400, completion_tokens=12, total_tokens=412, cost=0.0011)


def ask(client, **body):
    response = client.post("/chat/stream", json=body)
    return response, sse_events(response.text)


def test_answer_streams_sources_then_deltas_then_done(model):
    document_id = save_document("plan.pdf", ["Core habits: read code first."], pages=[5])
    model.stream = FakeStream([chunk("Read code first "), chunk("[1]."), chunk("", USAGE)])

    with TestClient(main.app) as client:
        response, events = ask(client, message="What are the core habits?")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [e["type"] for e in events] == ["sources", "delta", "delta", "done"]
    [first] = events[0]["sources"]
    assert first == {
        "n": 1,
        "document_id": document_id,
        "filename": "plan.pdf",
        "page": 5,
        "chunk_id": first["chunk_id"],
        "snippet": "Core habits: read code first.",
        "similarity": 1.0,
    }
    assert "".join(e["content"] for e in events if e["type"] == "delta") == "Read code first [1]."
    usage = events[-1]["usage"]
    assert (usage["kind"], usage["prompt_tokens"], usage["cost_usd"]) == ("chat", 400, 0.0011)
    # The model got the question and the numbered source.
    assert 'What are the core habits?' in model.requests[0]["messages"][-1]["content"]
    assert '<source id="1" file="plan.pdf" page="5">' in model.requests[0]["messages"][-1]["content"]


def test_document_ids_limit_the_sources(model):
    save_document("first.pdf", ["First document."])
    second = save_document("second.pdf", ["Second document.", "More of it."])
    model.stream = FakeStream([chunk("Answer."), chunk("", USAGE)])

    with TestClient(main.app) as client:
        _, events = ask(client, message="Question?", document_ids=[second])

    assert {s["document_id"] for s in events[0]["sources"]} == {second}
    assert [s["n"] for s in events[0]["sources"]] == [1, 2]


def test_empty_document_ids_search_everything(model):
    save_document("first.pdf", ["First document."])
    save_document("second.pdf", ["Second document."])
    model.stream = FakeStream([chunk("Answer."), chunk("", USAGE)])

    with TestClient(main.app) as client:
        _, events = ask(client, message="Question?", document_ids=[])

    assert {s["filename"] for s in events[0]["sources"]} == {"first.pdf", "second.pdf"}


def test_history_is_passed_to_the_model(model):
    save_document("plan.pdf", ["Some text."])
    model.stream = FakeStream([chunk("Answer."), chunk("", USAGE)])
    history = [
        {"role": "user", "content": "What are the core habits?"},
        {"role": "assistant", "content": "Read code first [1]."},
    ]

    with TestClient(main.app) as client:
        ask(client, message="And the second one?", history=history)

    messages = model.requests[0]["messages"]
    assert messages[1:3] == [
        {"role": "user", "content": "What are the core habits?"},
        {"role": "assistant", "content": "Read code first."},
    ]


@pytest.mark.parametrize("message", ["", "   \n "])
def test_empty_message_is_rejected(model, message):
    with TestClient(main.app) as client:
        response, _ = ask(client, message=message)

    assert response.status_code == 400
    assert response.json() == {"detail": "The message is empty."}
    assert model.requests == []


def test_unknown_document_ids_are_rejected(model):
    known = save_document("plan.pdf", ["Some text."])

    with TestClient(main.app) as client:
        response, _ = ask(client, message="Question?", document_ids=[known, 9998, 9999])

    # 404, like another user's document, so the answer never reveals what exists.
    assert response.status_code == 404
    assert response.json() == {"detail": "Document not found."}
    assert model.requests == []


def test_with_no_documents_the_fixed_reply_is_sent_without_a_model_call(model):
    with TestClient(main.app) as client:
        _, events = ask(client, message="What are the core habits?")

    assert events == [
        {"type": "sources", "sources": []},
        {"type": "delta", "content": chat.NOT_FOUND_MESSAGE},
        {"type": "done", "usage": None},
    ]
    assert model.requests == []


@pytest.mark.parametrize(
    ("provider_error", "status", "message"),
    [
        (APITimeoutError(request=REQUEST), 504, "The AI service took too long to respond."),
        (APIConnectionError(request=REQUEST), 503, "Could not connect to the AI service."),
        (
            RateLimitError("limited", response=httpx.Response(429, request=REQUEST), body=None),
            429,
            "The AI service rate limit has been exceeded. Please try again later.",
        ),
        (
            APIStatusError("bad", response=httpx.Response(400, request=REQUEST), body=None),
            502,
            "The AI service returned an API error: 400",
        ),
        (
            APIStatusError("no credit", response=httpx.Response(402, request=REQUEST), body=None),
            503,
            "The AI service is out of credit. Please try again later.",
        ),
    ],
    ids=["timeout-504", "connection-503", "rate-limit-429", "provider-502", "out-of-credit-503"],
)
def test_model_errors_become_sse_error_events(model, provider_error, status, message):
    save_document("plan.pdf", ["Some text."])
    model.stream = FakeStream([chunk("Partial")], error=provider_error)

    with TestClient(main.app) as client:
        response, events = ask(client, message="Question?")

    assert response.status_code == 200
    assert [e["type"] for e in events] == ["sources", "delta", "error"]
    assert events[-1] == {"type": "error", "status": status, "message": message}


def test_out_of_credit_before_the_answer_starts_is_a_503_after_one_call(monkeypatch):
    """The usual case: the provider refuses the request itself (not mid-stream)."""
    save_document("plan.pdf", ["Some text."])
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        raise APIStatusError("no credit", response=httpx.Response(402, request=REQUEST), body=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(chat, "_get_async_client", lambda: client)

    with TestClient(main.app) as test_client:
        response, events = ask(test_client, message="Question?")

    assert response.status_code == 200
    assert [e["type"] for e in events] == ["sources", "error"]
    assert events[-1] == {"type": "error", "status": 503, "message": "The AI service is out of credit. Please try again later."}
    assert len(requests) == 1  # never retried


def test_question_embedding_failure_sends_only_an_error(model, monkeypatch):
    save_document("plan.pdf", ["Some text."])

    async def failing_embed_query(text):
        raise main.AIServiceNotConfiguredError("AI service is not configured.")

    monkeypatch.setattr(chat, "embed_query", failing_embed_query)

    with TestClient(main.app) as client:
        _, events = ask(client, message="Question?")

    assert events == [{"type": "error", "status": 503, "message": "AI service is not configured."}]
    assert model.requests == []


def test_unexpected_errors_are_logged_and_reported(model, monkeypatch, caplog):
    def broken_search(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(chat, "find_matches", broken_search)

    with TestClient(main.app) as client:
        _, events = ask(client, message="Question?")

    assert events == [{"type": "error", "status": 500, "message": "Could not answer the question."}]
    assert "Answering a chat question failed." in caplog.text
    assert "database is down" in caplog.text


def test_client_disconnect_closes_the_model_stream(model, caplog):
    caplog.set_level(logging.INFO, logger="app.services.usage")
    save_document("plan.pdf", ["Some text."])
    stream = FakeStream([chunk("First "), chunk("second "), chunk("", USAGE)])
    model.stream = stream

    async def disconnect_after_first_delta():
        response = await main.chat_stream(
            main.ChatRequest(message="Question?"), user=User(DEFAULT_USER_ID, DEFAULT_USER_EMAIL)
        )
        events = [await anext(response.body_iterator), await anext(response.body_iterator)]
        await response.body_iterator.aclose()  # what a client disconnect does
        return events

    with TestClient(main.app):
        events = asyncio.run(disconnect_after_first_delta())

    assert '"type": "sources"' in events[0] and '"type": "delta"' in events[1]
    assert stream.closed
    [logged] = [r.ai_usage for r in caplog.records if hasattr(r, "ai_usage")]
    assert logged["kind"] == "chat" and logged["outcome"] == "cancelled"
