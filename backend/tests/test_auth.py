"""Accounts: register, login, logout, me; Argon2 hashing; sliding sessions in an httpOnly
cookie; rate limits; CSRF; and that no password or token is ever logged."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.auth as auth
import app.database as database
from app.main import app


pytestmark = pytest.mark.real_auth  # real users, no default signed-in user

ORIGIN = "http://localhost:3000"
PASSWORD = "correct horse battery"
START = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class Clock:
    def __init__(self):
        self.now = START

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(auth, "_now", lambda: clock.now)
    return clock


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    # Fast Argon2 in tests (still Argon2id); one test checks the real defaults.
    monkeypatch.setattr(auth, "_hasher", FAST_HASHER)
    monkeypatch.setattr(auth, "_dummy_hash", None)
    monkeypatch.setenv("ALLOWED_ORIGINS", f"{ORIGIN},https://app.example.com")
    monkeypatch.delenv("SESSION_DAYS", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)


@pytest.fixture
def client():
    # https, so the Secure cookie is stored and sent back like in a browser.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def post(client, path, body=None, origin=ORIGIN, **kwargs):
    headers = kwargs.pop("headers", {})
    if origin:
        headers["origin"] = origin
    return client.post(path, json=body, headers=headers, **kwargs)


def register(client, email="ana@example.com", password=PASSWORD, **kwargs):
    return post(client, "/auth/register", {"email": email, "password": password}, **kwargs)


def login(client, email="ana@example.com", password=PASSWORD, **kwargs):
    return post(client, "/auth/login", {"email": email, "password": password}, **kwargs)


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


def set_cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


def with_session(token: str) -> dict:
    """Send a given session token explicitly (httpx won't send a hand-set cookie)."""
    return {"cookie": f"{auth.COOKIE_NAME}={token}"}


# --- Passwords ----------------------------------------------------------------------------


def test_passwords_are_hashed_with_argon2id_and_verified():
    stored = auth.hash_password(PASSWORD)
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored
    assert auth.verify_password(stored, PASSWORD)
    assert not auth.verify_password(stored, "wrong password")


class CountingHasher:
    """The fast hasher, recording which hashes were verified."""

    def __init__(self):
        self.verified = []

    def hash(self, password):
        return FAST_HASHER.hash(password)

    def verify(self, password_hash, password):
        self.verified.append(password_hash)
        return FAST_HASHER.verify(password_hash, password)

    def check_needs_rehash(self, password_hash):
        return FAST_HASHER.check_needs_rehash(password_hash)


def test_an_unknown_account_is_checked_against_a_dummy_hash(monkeypatch):
    hasher = CountingHasher()
    monkeypatch.setattr(auth, "_hasher", hasher)

    assert auth.verify_password(None, PASSWORD) is False
    assert len(hasher.verified) == 1 and hasher.verified[0].startswith("$argon2id$")


def test_login_with_an_unknown_email_still_verifies_a_hash(client, monkeypatch):
    hasher = CountingHasher()
    monkeypatch.setattr(auth, "_hasher", hasher)
    assert login(client, email="nobody@example.com").status_code == 401
    assert len(hasher.verified) == 1


def test_a_broken_stored_hash_fails_closed_and_is_logged(caplog):
    assert auth.verify_password("not-a-hash", PASSWORD) is False
    assert "could not be checked" in caplog.text


def test_login_upgrades_an_outdated_hash(client, monkeypatch):
    register(client)  # stored with the fast (weaker) parameters
    old = sql("SELECT password_hash FROM users").scalar_one()
    monkeypatch.setattr(auth, "_hasher", PasswordHasher())  # the real defaults

    assert login(client).status_code == 200
    new = sql("SELECT password_hash FROM users").scalar_one()
    assert new != old and new.startswith("$argon2id$")
    assert not PasswordHasher().check_needs_rehash(new)


# --- Register -----------------------------------------------------------------------------


def test_register_creates_the_account_and_signs_in(client):
    response = register(client, email="  Ana@Example.COM ")

    assert response.status_code == 201
    assert response.json() == {"id": 1, "email": "ana@example.com"}
    cookie = set_cookie_header(response)
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", f"Max-Age={14 * 24 * 3600}"):
        assert flag in cookie
    assert client.get("/auth/me").json() == {"id": 1, "email": "ana@example.com"}

    stored = sql("SELECT email, password_hash FROM users").one()
    assert stored.email == "ana@example.com"
    assert stored.password_hash.startswith("$argon2id$") and PASSWORD not in stored.password_hash
    # Only the token's hash is stored, never the token itself.
    token = client.cookies.get(auth.COOKIE_NAME)
    stored_hash = sql("SELECT token_hash FROM sessions").scalar_one()
    assert bytes(stored_hash) == auth.token_hash(token) and token.encode() not in bytes(stored_hash)


def test_one_account_per_email_whatever_the_case(client):
    register(client, email="ana@example.com")
    response = register(client, email="ANA@example.com")
    assert response.status_code == 409
    assert response.json() == {"detail": "Could not create an account with this email."}


@pytest.mark.parametrize("email", ["", "ana", "ana@", "@example.com", "ana@example", "a b@example.com"])
def test_register_rejects_invalid_emails(client, email):
    response = register(client, email=email)
    assert response.status_code == 400
    assert response.json() == {"detail": "Enter a valid email address."}


@pytest.mark.parametrize("password", ["short", "x" * 9, "x" * 129])
def test_register_rejects_passwords_outside_10_to_128_characters(client, password):
    response = register(client, password=password)
    assert response.status_code == 400
    assert response.json() == {"detail": "The password must be 10 to 128 characters long."}
    assert sql("SELECT count(*) FROM users").scalar_one() == 0


@pytest.mark.parametrize("password", ["x" * 10, "x" * 128])
def test_register_accepts_passwords_of_10_and_128_characters(client, password):
    assert register(client, password=password).status_code == 201


# --- Login and me -------------------------------------------------------------------------


def test_login_signs_in_with_a_new_session(client):
    register(client)
    client.cookies.clear()
    assert client.get("/auth/me").status_code == 401

    response = login(client, email="ANA@example.com")

    assert response.status_code == 200
    assert response.json() == {"id": 1, "email": "ana@example.com"}
    assert "HttpOnly" in set_cookie_header(response)
    assert client.get("/auth/me").json()["email"] == "ana@example.com"
    assert sql("SELECT count(*) FROM sessions").scalar_one() == 2


def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(client):
    register(client)
    client.cookies.clear()

    wrong_password = login(client, password="not the password")
    unknown_email = login(client, email="nobody@example.com")

    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert response.json() == {"detail": "Email or password is incorrect."}
        assert "set-cookie" not in response.headers


def test_me_without_a_valid_cookie_is_401(client):
    assert client.get("/auth/me").status_code == 401
    register(client)
    real_token = client.cookies.get(auth.COOKIE_NAME)
    client.cookies.clear()
    # The real token works when sent this way, so the made-up one is a fair test.
    assert client.get("/auth/me", headers=with_session(real_token)).status_code == 200

    response = client.get("/auth/me", headers=with_session("made-up-token"))
    assert response.status_code == 401
    assert response.json() == {"detail": "Not signed in."}


# --- Logout and expiry --------------------------------------------------------------------


def test_logout_revokes_the_session_and_clears_the_cookie(client):
    register(client)
    token = client.cookies.get(auth.COOKIE_NAME)

    response = post(client, "/auth/logout")

    assert response.status_code == 204
    assert f'{auth.COOKIE_NAME}=""' in set_cookie_header(response)
    assert "Max-Age=0" in set_cookie_header(response)
    assert sql("SELECT revoked_at IS NOT NULL FROM sessions").scalar_one() is True
    # The old token no longer works, even if someone kept it.
    client.cookies.clear()
    assert client.get("/auth/me", headers=with_session(token)).status_code == 401


def test_logout_without_a_session_still_succeeds(client):
    assert post(client, "/auth/logout").status_code == 204


def test_sessions_that_ended_more_than_a_day_ago_are_deleted(client, clock):
    def sessions():
        return sql("SELECT u.email, s.revoked_at IS NOT NULL AS revoked FROM sessions s "
                   "JOIN users u ON u.id = s.user_id ORDER BY s.id").all()

    register(client, email="ana@example.com")
    post(client, "/auth/logout")                  # Ana's session: revoked at START
    register(client, email="ben@example.com")     # Ben's session: expires at START + 14 days
    client.cookies.clear()

    clock.advance(days=14, hours=12)               # revoked 14.5 days ago, expired 12 hours ago
    login(client, email="ana@example.com")         # creating a session cleans up

    assert [(r.email, r.revoked) for r in sessions()] == [
        ("ben@example.com", False),                # ended less than a day ago: kept
        ("ana@example.com", False),                # the new session
    ]

    clock.advance(hours=13)                        # Ben's session ended 25 hours ago
    client.cookies.clear()
    login(client, email="ana@example.com")

    assert [r.email for r in sessions()] == ["ana@example.com", "ana@example.com"]
    # The active session from 13 hours ago is untouched and still works.
    assert client.get("/auth/me").status_code == 200


def test_an_expired_session_is_401(client, clock):
    register(client)
    clock.advance(days=14, seconds=1)
    assert client.get("/auth/me").status_code == 401


# --- Sliding sessions ---------------------------------------------------------------------


def expires_at():
    return sql("SELECT expires_at FROM sessions").scalar_one()


def test_a_visit_within_5_minutes_does_not_renew_the_session(client, clock):
    register(client)
    before = expires_at()
    clock.advance(minutes=4)

    response = client.get("/auth/me")

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert expires_at() == before


def test_a_later_visit_renews_the_session_and_resends_the_cookie(client, clock):
    register(client)
    clock.advance(days=10)  # 4 days left

    response = client.get("/auth/me")

    assert response.status_code == 200
    cookie = set_cookie_header(response)
    assert f"Max-Age={14 * 24 * 3600}" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert expires_at() == clock.now + timedelta(days=14)
    last_used = sql("SELECT last_used_at FROM sessions").scalar_one()
    assert last_used == clock.now
    # Still signed in 13 days later, because the session slid forward.
    clock.advance(days=13)
    assert client.get("/auth/me").status_code == 200


def test_a_session_idle_for_more_than_14_days_ends(client, clock):
    register(client)
    clock.advance(days=10)
    client.get("/auth/me")  # renewed: expires 14 days from here
    clock.advance(days=14, seconds=1)
    assert client.get("/auth/me").status_code == 401


def test_session_days_is_configurable(client, clock, monkeypatch):
    monkeypatch.setenv("SESSION_DAYS", "2")
    response = register(client)
    assert f"Max-Age={2 * 24 * 3600}" in set_cookie_header(response)
    clock.advance(days=2, seconds=1)
    assert client.get("/auth/me").status_code == 401


def test_a_renewed_cookie_reaches_a_streamed_response_unchanged(client, clock):
    """Chat and uploads stream; the renewed cookie must arrive with them, and the stream
    must pass through the middleware as it is."""
    register(client)
    clock.advance(minutes=10)
    token = client.cookies.get(auth.COOKIE_NAME)

    streaming_app = FastAPI()
    streaming_app.add_middleware(auth.SessionCookieMiddleware)

    @streaming_app.get("/stream")
    async def stream(user: auth.User = Depends(auth.current_user)):
        async def events():
            for n in range(3):
                await asyncio.sleep(0)
                yield f"data: {n}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    with TestClient(streaming_app, base_url="https://testserver") as stream_client:
        response = stream_client.get("/stream", headers=with_session(token))

    assert response.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"Max-Age={14 * 24 * 3600}" in set_cookie_header(response)


# --- Rate limits --------------------------------------------------------------------------


def test_five_failed_logins_block_that_email_even_with_the_right_password(client, clock):
    register(client)
    client.cookies.clear()
    for _ in range(5):
        assert login(client, password="wrong password").status_code == 401

    blocked = login(client)  # the right password

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == str(15 * 60)
    assert blocked.json() == {
        "detail": "Too many failed sign-in attempts. Try again in 15 minutes."
    }
    assert client.get("/auth/me").status_code == 401
    # Other accounts are not affected.
    register(client, email="ben@example.com")
    client.cookies.clear()
    assert login(client, email="ben@example.com").status_code == 200

    clock.advance(minutes=15, seconds=1)
    assert login(client).status_code == 200


def test_retry_after_counts_down_from_the_oldest_failure(client, clock):
    register(client)
    for _ in range(5):
        login(client, password="wrong password")
        clock.advance(minutes=1)
    # The oldest failure was 5 minutes ago, so it falls out of the window in 10.
    response = login(client)
    assert response.headers["retry-after"] == str(10 * 60)
    assert "Try again in 10 minutes." in response.json()["detail"]


def test_twenty_failed_logins_from_one_ip_block_that_ip(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    attacker = {"x-vercel-forwarded-for": "203.0.113.7"}
    for n in range(20):
        response = login(client, email=f"user{n}@example.com", password="guess", headers=dict(attacker))
        assert response.status_code == 401

    assert login(client, email="new@example.com", headers=dict(attacker)).status_code == 429
    # Another address is not blocked.
    other = {"x-vercel-forwarded-for": "198.51.100.9"}
    assert login(client, email="new@example.com", headers=other).status_code == 401


def test_the_ip_comes_from_vercels_header_only_on_vercel(monkeypatch):
    request = type("R", (), {
        "headers": {"x-vercel-forwarded-for": "203.0.113.7, 10.0.0.1"},
        "client": type("C", (), {"host": "10.1.2.3"})(),
    })()
    monkeypatch.delenv("VERCEL", raising=False)
    assert auth.client_ip(request) == "10.1.2.3"  # locally the header could be faked
    monkeypatch.setenv("VERCEL", "1")
    assert auth.client_ip(request) == "203.0.113.7"


def test_five_registrations_per_ip_per_day(client, clock):
    for n in range(5):
        status = register(client, email=f"user{n}@example.com").status_code
        assert status == 201
    blocked = register(client, email="user5@example.com")
    assert blocked.status_code == 429
    assert "Too many registration attempts" in blocked.json()["detail"]
    assert int(blocked.headers["retry-after"]) == 24 * 3600

    clock.advance(days=1, seconds=1)
    assert register(client, email="user5@example.com").status_code == 201


def test_invalid_registration_attempts_count_too(client):
    for _ in range(5):
        assert register(client, email="not an email").status_code == 400
    assert register(client).status_code == 429
    outcomes = sql("SELECT succeeded FROM login_attempts WHERE kind = 'register'").scalars().all()
    assert outcomes == [False] * 5


def test_old_attempts_are_deleted(client, clock):
    login(client, password="wrong password")
    clock.advance(days=1, minutes=1)
    login(client, password="wrong password")
    assert sql("SELECT count(*) FROM login_attempts").scalar_one() == 1


# --- CSRF ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/auth/register", "/auth/login", "/auth/logout"])
def test_requests_without_an_allowed_origin_are_refused(client, path):
    body = {"email": "ana@example.com", "password": PASSWORD}
    for origin in (None, "https://evil.example.com"):
        response = post(client, path, body, origin=origin)
        assert response.status_code == 403
        assert response.json() == {"detail": "Request origin not allowed."}
    assert sql("SELECT count(*) FROM users").scalar_one() == 0


def test_referer_is_accepted_when_origin_is_missing(client):
    response = post(client, "/auth/register", {"email": "ana@example.com", "password": PASSWORD},
                    origin=None, headers={"referer": f"{ORIGIN}/register?next=/chat"})
    assert response.status_code == 201


def test_allowed_origins_fall_back_to_cors_origins(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com/, https://preview.example.com")
    assert auth.allowed_origins() == {"https://app.example.com", "https://preview.example.com"}


def test_me_needs_no_origin(client):
    register(client)
    assert client.get("/auth/me").status_code == 200  # reads change nothing


# --- No secrets in logs -------------------------------------------------------------------


def test_passwords_tokens_and_full_emails_are_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    register(client, email="ana@example.com", password=PASSWORD)
    token = client.cookies.get(auth.COOKIE_NAME)
    login(client, email="ana@example.com", password="wrong password secret")
    login(client, email="ana@example.com", password=PASSWORD)
    post(client, "/auth/logout")

    logged = caplog.text
    assert PASSWORD not in logged
    assert "wrong password secret" not in logged
    assert token not in logged
    assert "ana@example.com" not in logged
    assert "Failed sign-in for an account at example.com" in logged
