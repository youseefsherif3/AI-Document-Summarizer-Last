"""Query rewriting for follow-up questions. No AI calls: the model is a fake."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

import app.services.chat as chat
import app.services.llm as llm
import app.services.query_rewrite as query_rewrite
from app.services.chat import HistoryMessage
from app.services.query_rewrite import EmptyRewriteError
from app.services.summarizer import AIServiceError

HISTORY = [
    HistoryMessage("user", "How many days of annual leave do Northwind employees get?"),
    HistoryMessage("assistant", "Northwind employees get 24 days [1]."),
]
USAGE = SimpleNamespace(prompt_tokens=120, completion_tokens=12, total_tokens=132, cost=0.0)


@pytest.fixture
def fake_model(monkeypatch):
    """A fake OpenRouter client for llm.complete; records every request."""
    state = {"requests": [], "reply": "How many days of annual leave do Brightwater employees get?"}

    async def create(**kwargs):
        state["requests"].append(kwargs)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        message = SimpleNamespace(content=state["reply"])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=USAGE)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_get_async_client", lambda: client)
    return state


# --- The rewrite call -------------------------------------------------------------


def test_without_history_no_call_is_made(fake_model):
    assert asyncio.run(query_rewrite.rewrite_question("And Brightwater?", [], "m/a")) == "And Brightwater?"
    assert fake_model["requests"] == []


def test_history_is_rewritten_with_one_logged_call(fake_model, caplog):
    caplog.set_level(logging.INFO, logger="app.services.usage")

    result = asyncio.run(query_rewrite.rewrite_question("And Brightwater?", HISTORY, "m/rewrite"))

    assert result == "How many days of annual leave do Brightwater employees get?"
    [request] = fake_model["requests"]
    assert request["model"] == "m/rewrite"
    assert request["temperature"] == 0
    # Reasoning off, with room left in case a model reasons anyway.
    assert request["max_tokens"] == query_rewrite.REWRITE_MAX_TOKENS >= 1000
    assert request["extra_body"] == {"reasoning": {"enabled": False}}
    system, user = request["messages"]
    assert system == {"role": "system", "content": query_rewrite.REWRITE_SYSTEM_PROMPT}
    assert "User: How many days of annual leave do Northwind employees get?" in user["content"]
    assert "Assistant: Northwind employees get 24 days [1]." in user["content"]
    assert user["content"].endswith("Last question: And Brightwater?\n\nStandalone question:")
    # Usage is logged like every other AI call.
    assert "ai_usage kind=rewrite model=m/rewrite outcome=ok prompt_tokens=120" in caplog.text


def test_only_the_last_six_history_messages_are_sent(fake_model):
    history = [HistoryMessage("user" if i % 2 == 0 else "assistant", f"Message {i}") for i in range(10)]

    asyncio.run(query_rewrite.rewrite_question("Next?", history, "m/a"))

    content = fake_model["requests"][0]["messages"][1]["content"]
    assert "Message 3" not in content and "Message 4" in content and "Message 9" in content


@pytest.mark.parametrize(("reply", "expected"), [
    ('"Who owns RouteWise?"', "Who owns RouteWise?"),
    ("Standalone question: Who owns RouteWise?\nIt was the second tool.", "Who owns RouteWise?"),
    ("\n  Who owns RouteWise?  \n", "Who owns RouteWise?"),
    ("", ""),
    ('  ""  \n', ""),
])
def test_replies_are_cleaned(reply, expected):
    assert query_rewrite.clean_rewrite(reply) == expected


@pytest.mark.parametrize("reply", ["", None, '""'])
def test_an_empty_reply_is_an_error_not_a_silent_fallback(fake_model, reply):
    # A reasoning model that uses its whole reply on reasoning returns no text.
    fake_model["reply"] = reply

    with pytest.raises(EmptyRewriteError, match="returned no text"):
        asyncio.run(query_rewrite.rewrite_question("And Brightwater?", HISTORY, "m/a"))


def test_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("QUERY_REWRITE_ENABLED", raising=False)
    assert query_rewrite.query_rewrite_enabled() is False
    for value in ("true", "1", "ON"):
        monkeypatch.setenv("QUERY_REWRITE_ENABLED", value)
        assert query_rewrite.query_rewrite_enabled() is True
    monkeypatch.setenv("QUERY_REWRITE_ENABLED", "false")
    assert query_rewrite.query_rewrite_enabled() is False


# --- In the chat flow --------------------------------------------------------------


@pytest.fixture
def searched(monkeypatch):
    """Records the text retrieval searches with."""
    queries = []

    async def fake_retrieve(question, document_ids=None):
        queries.append(question)
        return []

    monkeypatch.setattr(chat, "retrieve_sources", fake_retrieve)
    return queries


def test_chat_searches_with_the_rewritten_question_but_answers_the_original(
    fake_model, searched, monkeypatch
):
    monkeypatch.setenv("QUERY_REWRITE_ENABLED", "true")
    monkeypatch.setenv("QUERY_REWRITE_MODEL", "m/rewrite")

    prepared = asyncio.run(chat.prepare_answer("And Brightwater?", HISTORY))

    assert searched == ["How many days of annual leave do Brightwater employees get?"]
    assert prepared.search_query == searched[0]
    assert prepared.messages[-1]["content"].endswith("Question: And Brightwater?")
    assert fake_model["requests"][0]["model"] == "m/rewrite"


def test_rewrite_model_falls_back_to_the_chat_model(fake_model, searched, monkeypatch):
    monkeypatch.setenv("QUERY_REWRITE_ENABLED", "true")
    monkeypatch.delenv("QUERY_REWRITE_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "m/chat")

    asyncio.run(chat.prepare_answer("And Brightwater?", HISTORY))

    assert fake_model["requests"][0]["model"] == "m/chat"


def test_chat_makes_no_rewrite_call_when_disabled_or_without_history(fake_model, searched, monkeypatch):
    monkeypatch.delenv("QUERY_REWRITE_ENABLED", raising=False)
    asyncio.run(chat.prepare_answer("And Brightwater?", HISTORY))
    monkeypatch.setenv("QUERY_REWRITE_ENABLED", "true")
    prepared = asyncio.run(chat.prepare_answer("How long is parental leave?", []))

    assert fake_model["requests"] == []
    assert searched == ["And Brightwater?", "How long is parental leave?"]
    assert prepared.search_query is None


@pytest.mark.parametrize("error", [AIServiceError("bad gateway"), TimeoutError("slow"), ""])
def test_a_failed_rewrite_is_logged_and_the_original_question_is_used(
    fake_model, searched, monkeypatch, caplog, error
):
    monkeypatch.setenv("QUERY_REWRITE_ENABLED", "true")
    fake_model["reply"] = error  # "" = an empty reply

    asyncio.run(chat.prepare_answer("And Brightwater?", HISTORY))

    assert searched == ["And Brightwater?"]
    assert "Query rewrite failed; searching with the original question." in caplog.text
