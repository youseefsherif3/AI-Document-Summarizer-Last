"""Who and what the current work is for, carried along without passing it through every call.

`use_context(user_id=…, job_id=…)` marks a stretch of code (a chat answer, an upload, a worker
job). AI usage records get the user id, and every log line of the app gets the job id. The
values live in context variables, so concurrent requests and tasks never see each other's, and
`asyncio.run` / `create_task` copy them into the work they start.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_job_id: ContextVar[str | None] = ContextVar("current_job_id", default=None)


@contextmanager
def use_context(*, user_id: int | None = None, job_id: str | None = None):
    user_token = current_user_id.set(user_id) if user_id is not None else None
    job_token = current_job_id.set(job_id) if job_id is not None else None
    try:
        yield
    finally:
        if job_token is not None:
            current_job_id.reset(job_token)
        if user_token is not None:
            current_user_id.reset(user_token)


_installed = False


def install_log_context() -> None:
    """Add ` job_id=…` to every log line of the app made while a job is being worked on, so one
    job can be followed through the API, the worker and the AI calls with a single search.
    Lines that already name the job are left as they are."""
    global _installed
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        job_id = current_job_id.get()
        if job_id and record.name.startswith("app"):
            message = record.getMessage()
            if "job_id=" not in message:
                record.msg, record.args = f"{message} job_id={job_id}", ()
        return record

    logging.setLogRecordFactory(factory)
