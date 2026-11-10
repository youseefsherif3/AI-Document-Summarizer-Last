"""Accounts: email + password sign-in with sessions in an httpOnly cookie.

- Passwords are hashed with Argon2id (argon2-cffi defaults). Unknown emails are checked
  against a dummy hash, so a wrong email and a wrong password take about as long and get the
  same answer.
- A session is a random token in the `documind_session` cookie (httpOnly, Secure,
  SameSite=Lax). Only its SHA-256 is stored. Sessions slide: a visit more than
  SESSION_REFRESH_SECONDS after the last one moves expiry SESSION_DAYS forward and re-sends
  the cookie (SessionCookieMiddleware, which also works for streamed responses).
- Failed sign-ins are limited per email and per client IP, registrations per IP.
- Requests that change something must come from an allowed origin (CSRF check).
- Passwords, tokens and request bodies are never logged.
"""

import hashlib
import logging
import math
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import get_connection

logger = logging.getLogger(__name__)

COOKIE_NAME = "documind_session"
SESSION_REFRESH_SECONDS = 5 * 60
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
EMAIL_MAX_LENGTH = 254
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Rate limits.
LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILED_LOGINS_PER_EMAIL = 5
MAX_FAILED_LOGINS_PER_IP = 20
REGISTER_WINDOW = timedelta(days=1)
MAX_REGISTRATIONS_PER_IP = 5
ATTEMPTS_KEPT = timedelta(days=1)
# Expired or revoked sessions are deleted this long after they ended.
ENDED_SESSIONS_KEPT = timedelta(days=1)

DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"
)

WRONG_CREDENTIALS = "Email or password is incorrect."
NOT_SIGNED_IN = "Not signed in."

_hasher = PasswordHasher()
_dummy_hash: str | None = None


def _now() -> datetime:
    """The current time; a separate function so tests can move the clock."""
    return datetime.now(timezone.utc)


def session_days() -> int:
    return int(os.getenv("SESSION_DAYS", "14") or 14)


def allowed_origins() -> set[str]:
    """ALLOWED_ORIGINS, or CORS_ORIGINS, or the local frontend addresses."""
    raw = (
        os.getenv("ALLOWED_ORIGINS", "").strip()
        or os.getenv("CORS_ORIGINS", "").strip()
        or DEFAULT_ALLOWED_ORIGINS
    )
    return {origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()}


# --- Passwords ----------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """True if the password matches. With no hash (unknown email), a dummy hash is checked
    so the time taken doesn't reveal whether the account exists."""
    global _dummy_hash
    real_account = password_hash is not None
    if not real_account:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash(secrets.token_urlsafe(16))
        password_hash = _dummy_hash
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError):
        logger.error("A stored password hash could not be checked.")
        return False
    return real_account


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# --- Input --------------------------------------------------------------------------------


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= EMAIL_MAX_LENGTH and bool(_EMAIL.match(email))


def email_domain(email: str) -> str:
    """For logs: the part after @, never the whole address."""
    return email.rsplit("@", 1)[-1] if "@" in email else "(invalid)"


def client_ip(request: Request) -> str | None:
    """The client's IP. On Vercel, X-Vercel-Forwarded-For carries it (Vercel overwrites any
    value a client sends, also through the frontend's /api rewrite). Elsewhere, the socket."""
    if os.getenv("VERCEL"):
        forwarded = request.headers.get("x-vercel-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


# --- Sessions -----------------------------------------------------------------------------


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class User:
    id: int
    email: str


def _create_session(user_id: int, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    with get_connection() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (user_id, token_hash, created_at, last_used_at, expires_at) "
                "VALUES (:user_id, :hash, :now, :now, :expires)"
            ),
            {"user_id": user_id, "hash": token_hash(token), "now": now,
             "expires": now + timedelta(days=session_days())},
        )
        # Housekeeping, like old login attempts: sessions that ended more than a day ago.
        connection.execute(
            text("DELETE FROM sessions WHERE expires_at < :cutoff OR revoked_at < :cutoff"),
            {"cutoff": now - ENDED_SESSIONS_KEPT},
        )
    return token


def _session_user(token: str, now: datetime) -> tuple[User, bool] | None:
    """The signed-in user and whether the session was renewed now, or None."""
    with get_connection() as connection:
        row = connection.execute(
            text(
                "SELECT s.id AS session_id, s.last_used_at, u.id AS user_id, u.email "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = :hash AND s.revoked_at IS NULL AND s.expires_at > :now"
            ),
            {"hash": token_hash(token), "now": now},
        ).mappings().first()
        if row is None:
            return None
        renewed = (now - row["last_used_at"]).total_seconds() >= SESSION_REFRESH_SECONDS
        if renewed:
            connection.execute(
                text("UPDATE sessions SET last_used_at = :now, expires_at = :expires "
                     "WHERE id = :id"),
                {"now": now, "expires": now + timedelta(days=session_days()),
                 "id": row["session_id"]},
            )
    return User(id=row["user_id"], email=row["email"]), renewed


def _revoke_session(token: str, now: datetime) -> None:
    with get_connection() as connection:
        connection.execute(
            text("UPDATE sessions SET revoked_at = :now "
                 "WHERE token_hash = :hash AND revoked_at IS NULL"),
            {"hash": token_hash(token), "now": now},
        )


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=session_days() * 24 * 60 * 60,
        httponly=True, secure=True, samesite="lax", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="lax", path="/")


async def current_user(request: Request) -> User:
    """Dependency: the signed-in user, or 401. Renews the session (and its cookie) when due."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail=NOT_SIGNED_IN)
    found = await run_in_threadpool(_session_user, token, _now())
    if found is None:
        raise HTTPException(status_code=401, detail=NOT_SIGNED_IN)
    user, renewed = found
    if renewed:
        # Sent by SessionCookieMiddleware, so it also reaches streamed responses.
        request.state.renewed_session_cookie = token
    return user


class SessionCookieMiddleware:
    """Adds the renewed session cookie to whatever response the endpoint returns.

    Pure ASGI (not BaseHTTPMiddleware): it only adds a header to the response start, so
    streamed responses (chat, uploads) pass through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                token = scope.get("state", {}).get("renewed_session_cookie")
                if token:
                    cookie = Response()
                    set_session_cookie(cookie, token)
                    message.setdefault("headers", [])
                    message["headers"] = [
                        *message["headers"],
                        *[(k, v) for k, v in cookie.raw_headers if k == b"set-cookie"],
                    ]
            await send(message)

        await self.app(scope, receive, send_with_cookie)


# --- Rate limits --------------------------------------------------------------------------


def _record_attempt(kind: str, email: str | None, ip: str | None, succeeded: bool,
                    now: datetime) -> None:
    with get_connection() as connection:
        connection.execute(
            text("INSERT INTO login_attempts (kind, email, ip, succeeded, created_at) "
                 "VALUES (:kind, :email, :ip, :succeeded, :now)"),
            {"kind": kind, "email": email, "ip": ip, "succeeded": succeeded, "now": now},
        )
        connection.execute(
            text("DELETE FROM login_attempts WHERE created_at < :cutoff"),
            {"cutoff": now - ATTEMPTS_KEPT},
        )


def _retry_after(oldest: datetime, window: timedelta, now: datetime) -> int:
    return max(1, math.ceil((oldest + window - now).total_seconds()))


def _login_blocked(email: str, ip: str | None, now: datetime) -> int | None:
    """Seconds to wait if failed sign-ins for this email or IP are over the limit, else None."""
    since = now - LOGIN_WINDOW
    checks = [("email", email, MAX_FAILED_LOGINS_PER_EMAIL)]
    if ip:
        checks.append(("ip", ip, MAX_FAILED_LOGINS_PER_IP))
    with get_connection() as connection:
        for column, value, limit in checks:
            count, oldest = connection.execute(
                text(f"SELECT count(*), min(created_at) FROM login_attempts "
                     f"WHERE kind = 'login' AND NOT succeeded AND {column} = :value "
                     f"AND created_at > :since"),
                {"value": value, "since": since},
            ).one()
            if count >= limit:
                return _retry_after(oldest, LOGIN_WINDOW, now)
    return None


def _registration_blocked(ip: str | None, now: datetime) -> int | None:
    if not ip:
        return None
    with get_connection() as connection:
        count, oldest = connection.execute(
            text("SELECT count(*), min(created_at) FROM login_attempts "
                 "WHERE kind = 'register' AND ip = :ip AND created_at > :since"),
            {"ip": ip, "since": now - REGISTER_WINDOW},
        ).one()
    if count >= MAX_REGISTRATIONS_PER_IP:
        return _retry_after(oldest, REGISTER_WINDOW, now)
    return None


def _too_many(message: str, retry_after: int) -> HTTPException:
    minutes = max(1, math.ceil(retry_after / 60))
    return HTTPException(
        status_code=429,
        detail=f"{message} Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
        headers={"Retry-After": str(retry_after)},
    )


# --- CSRF ---------------------------------------------------------------------------------


async def require_trusted_origin(request: Request) -> None:
    """Dependency for requests that change something: they must come from the frontend.

    Browsers send Origin on POST/DELETE (Referer as a fallback). Together with the
    SameSite=Lax cookie, this stops other sites from acting with a user's session.
    """
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer", "")
        match = re.match(r"^(https?://[^/]+)", referer)
        origin = match.group(1) if match else None
    if not origin or origin.rstrip("/") not in allowed_origins():
        logger.warning("Refused a %s %s from origin %r.", request.method, request.url.path, origin)
        raise HTTPException(status_code=403, detail="Request origin not allowed.")


# --- Users --------------------------------------------------------------------------------


class EmailTakenError(Exception):
    pass


def _create_user(email: str, password_hash: str, now: datetime) -> int:
    try:
        with get_connection() as connection:
            return connection.execute(
                text("INSERT INTO users (email, password_hash, created_at) "
                     "VALUES (:email, :hash, :now) RETURNING id"),
                {"email": email, "hash": password_hash, "now": now},
            ).scalar_one()
    except IntegrityError as error:
        if "uq_users_email" in str(error.orig):
            raise EmailTakenError() from error
        raise


def _user_by_email(email: str) -> tuple[int, str] | None:
    with get_connection() as connection:
        row = connection.execute(
            text("SELECT id, password_hash FROM users WHERE email = :email"), {"email": email}
        ).first()
    return (row[0], row[1]) if row else None


def _update_password_hash(user_id: int, password_hash: str) -> None:
    with get_connection() as connection:
        connection.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": password_hash, "id": user_id},
        )


# --- Endpoints ----------------------------------------------------------------------------


class Credentials(BaseModel):
    email: str = Field(max_length=1000)
    # Longer than allowed, so a too-long password gets our message instead of a 422.
    password: str = Field(max_length=1000)


router = APIRouter(prefix="/auth", tags=["auth"])


def _signed_in(user_id: int, email: str, token: str, status_code: int) -> JSONResponse:
    response = JSONResponse({"id": user_id, "email": email}, status_code=status_code)
    set_session_cookie(response, token)
    return response


@router.post("/register", dependencies=[Depends(require_trusted_origin)])
async def register(credentials: Credentials, request: Request):
    now = _now()
    ip = client_ip(request)
    email = normalize_email(credentials.email)

    blocked = await run_in_threadpool(_registration_blocked, ip, now)
    if blocked:
        raise _too_many("Too many registration attempts from this network.", blocked)

    created = False
    try:
        if not valid_email(email):
            raise HTTPException(status_code=400, detail="Enter a valid email address.")
        if not PASSWORD_MIN_LENGTH <= len(credentials.password) <= PASSWORD_MAX_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"The password must be {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} "
                       "characters long.",
            )
        password_hash = await run_in_threadpool(hash_password, credentials.password)
        try:
            user_id = await run_in_threadpool(_create_user, email, password_hash, now)
        except EmailTakenError:
            raise HTTPException(
                status_code=409, detail="Could not create an account with this email."
            ) from None
        token = await run_in_threadpool(_create_session, user_id, now)
        created = True
    finally:
        # Every attempt counts towards the per-IP limit, successful or not.
        await run_in_threadpool(
            _record_attempt, "register", email[:EMAIL_MAX_LENGTH], ip, created, now
        )
    logger.info("Account %s created.", user_id)
    return _signed_in(user_id, email, token, 201)


@router.post("/login", dependencies=[Depends(require_trusted_origin)])
async def login(credentials: Credentials, request: Request):
    now = _now()
    ip = client_ip(request)
    email = normalize_email(credentials.email)[:EMAIL_MAX_LENGTH]

    # Checked before the password, so a guessed password can't get through a block.
    blocked = await run_in_threadpool(_login_blocked, email, ip, now)
    if blocked:
        raise _too_many("Too many failed sign-in attempts.", blocked)

    found = await run_in_threadpool(_user_by_email, email) if valid_email(email) else None
    user_id, stored_hash = found if found else (None, None)
    if not await run_in_threadpool(verify_password, stored_hash, credentials.password):
        await run_in_threadpool(_record_attempt, "login", email, ip, False, now)
        logger.warning("Failed sign-in for an account at %s from %s.", email_domain(email), ip)
        raise HTTPException(status_code=401, detail=WRONG_CREDENTIALS)

    await run_in_threadpool(_record_attempt, "login", email, ip, True, now)
    if needs_rehash(stored_hash):
        await run_in_threadpool(
            _update_password_hash, user_id, await run_in_threadpool(hash_password, credentials.password)
        )
    token = await run_in_threadpool(_create_session, user_id, now)
    return _signed_in(user_id, email, token, 200)


@router.post("/logout", status_code=204, dependencies=[Depends(require_trusted_origin)])
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await run_in_threadpool(_revoke_session, token, _now())
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email}
