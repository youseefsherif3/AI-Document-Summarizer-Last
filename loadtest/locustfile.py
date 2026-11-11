"""One Locust scenario: signed-in users list their documents, upload a small file (queue mode)
and wait for its job, and ask chat questions.

    locust -f loadtest/locustfile.py --headless -u 20 -r 5 -t 60s --host http://127.0.0.1:8000

Needs the load-test stack (loadtest/README.md): the API and worker in FAKE LLM mode, and users
from loadtest/seed_users.py. The test REFUSES TO RUN unless it is sure no real AI call can be
made: the host must be this machine, `/health` must say `fake_llm`, and the first finished upload
must carry the fake marker (if the worker were using a real provider, the test stops at once).

Besides Locust's per-request numbers it records:
- "job: upload -> completed": time from the upload request to the finished job (queue wait + work)
- "chat: first event" and "chat: complete": time to the first streamed event / to the end
and, every 2 seconds, the queue depth and job counts from /metrics (to LOADTEST_SAMPLES_FILE).
"""

import csv
import json
import os
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import gevent
import requests
from locust import HttpUser, between, events, task

FAKE_MARKER = "[FAKE LLM]"
ORIGIN = "http://localhost:3000"  # an origin the API's CSRF check allows
METRICS_TOKEN = os.getenv("LOADTEST_METRICS_TOKEN", "loadtest-token")
USERS = json.loads(Path(__file__).with_name("users.json").read_text(encoding="utf-8"))["users"] \
    if Path(__file__).with_name("users.json").exists() else []
POLL_SECONDS = 1.5  # like the browser
JOB_TIMEOUT_SECONDS = 180

WORDS = ("contract invoice refund policy shipping warranty customer payment delivery schedule "
         "report budget meeting project deadline supplier quality safety training review").split()
QUESTIONS = [
    "What is the refund policy?", "When is the delivery scheduled?", "Who is the supplier?",
    "What does the warranty cover?", "Summarize the payment terms.", "What is the project deadline?",
    "What training is required?", "Which safety rules apply?", "What was decided in the meeting?",
]

_next_user = 0
_stop_reason = None


def small_document() -> bytes:
    """About 2 KB of text that is different every time, so nothing can be served from a cache."""
    rng = random.Random()
    return (" ".join(rng.choice(WORDS) for _ in range(300)) + f" ref-{rng.random()}").encode()


def fire(name: str, started: float, exception=None, kind: str = "STEP") -> None:
    events.request.fire(
        request_type=kind, name=name, response_time=(time.perf_counter() - started) * 1000,
        response_length=0, exception=exception, context={},
    )


@events.test_start.add_listener
def check_it_is_safe(environment, **kwargs):
    """Refuse to run against anything but a local fake-LLM stack."""
    host = urlparse(environment.host or "").hostname
    problem = None
    if host not in ("127.0.0.1", "localhost"):
        problem = f"the host must be this machine (127.0.0.1 or localhost), got {host!r}"
    elif not USERS:
        problem = "no users: run `python loadtest/seed_users.py --users N` first"
    else:
        try:
            health = requests.get(f"{environment.host}/health", timeout=5).json()
        except Exception as error:  # noqa: BLE001
            problem = f"cannot reach {environment.host}/health ({type(error).__name__})"
        else:
            if health.get("fake_llm") is not True:
                problem = "the backend is NOT in FAKE_LLM mode; a real AI key could be used"
    if problem:
        print(f"\nLOAD TEST REFUSED: {problem}\n")
        environment.process_exit_code = 3
        environment.runner.quit()
        return
    samples = os.getenv("LOADTEST_SAMPLES_FILE")
    if samples:
        environment.sampler = gevent.spawn(sample_metrics, environment.host, samples)


@events.test_stop.add_listener
def stop_sampler(environment, **kwargs):
    sampler = getattr(environment, "sampler", None)
    if sampler:
        sampler.kill()


def sample_metrics(host: str, path: str) -> None:
    """Every 2 s: queue depth and queue job counts from /metrics, to a CSV."""
    started = time.time()
    with open(path, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["seconds", "queue_depth", "pending", "processing", "completed", "failed"])
        while True:
            row = {"queue_depth": "", "pending": "", "processing": "", "completed": "", "failed": ""}
            try:
                body = requests.get(f"{host}/metrics", timeout=5,
                                    headers={"Authorization": f"Bearer {METRICS_TOKEN}"}).text
                for line in body.splitlines():
                    if line.startswith("documind_queue_depth "):
                        row["queue_depth"] = line.split()[-1]
                    elif line.startswith('documind_jobs{mode="queue",state="'):
                        state = line.split('state="')[1].split('"')[0]
                        row[state] = line.split()[-1]
            except Exception:  # noqa: BLE001  (a missed sample is just a gap)
                pass
            out.writerow([round(time.time() - started, 1), *row.values()])
            handle.flush()
            gevent.sleep(2)


class DocumentUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        global _next_user
        account = USERS[_next_user % len(USERS)]
        _next_user += 1
        self.client.headers.update({
            "Cookie": f"documind_session={account['token']}", "Origin": ORIGIN,
        })
        self.upload_and_wait()  # so there is something to ask about

    def abort_run(self, reason: str):
        global _stop_reason
        if _stop_reason is None:
            _stop_reason = reason
            print(f"\nLOAD TEST STOPPED: {reason}\n")
            self.environment.process_exit_code = 3
            self.environment.runner.quit()

    @task(4)
    def list_documents(self):
        self.client.get("/documents", name="GET /documents")

    @task(1)
    def upload_and_wait(self):
        started = time.perf_counter()
        with self.client.post(
            "/documents/upload/stream", name="POST /documents/upload/stream (queue)",
            files={"file": (f"load-{random.randrange(10**9)}.txt", small_document(), "text/plain")},
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"expected 202, got {response.status_code}: {response.text[:120]}")
                return
            job_id = response.json()["job_id"]
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            gevent.sleep(POLL_SECONDS)
            with self.client.get(f"/jobs/{job_id}", name="GET /jobs/{id}", catch_response=True) as poll:
                if poll.status_code != 200:
                    poll.failure(f"HTTP {poll.status_code}")
                    fire("job: upload -> completed", started, Exception("poll failed"))
                    return
                job = poll.json()
            if job["status"] == "completed":
                if not (job.get("summary") or "").startswith(FAKE_MARKER):
                    self.abort_run("a finished job's summary is not the fake one: the worker may "
                                   "be using a real AI provider")
                    return
                fire("job: upload -> completed", started)
                return
            if job["status"] == "failed":
                fire("job: upload -> completed", started,
                     Exception(f"job failed: {(job.get('error') or {}).get('code')}"))
                return
        fire("job: upload -> completed", started, Exception("timed out waiting for the job"))

    @task(4)
    def chat(self):
        started = time.perf_counter()
        first = None
        failure = None
        with self.client.post(
            "/chat/stream", name="POST /chat/stream (headers)",
            json={"message": random.choice(QUESTIONS), "history": []},
            stream=True, catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:120]}")
                return
            done = False
            for line in response.iter_lines():
                if not line.startswith(b"data: "):
                    continue
                if first is None:
                    first = time.perf_counter()
                event = json.loads(line[6:])
                if event["type"] == "error":
                    failure = f"error event: {event.get('message')}"
                    break
                if event["type"] == "done":
                    done = True
            if failure is None and not done:
                failure = "the stream ended without a done event"
            if failure:
                response.failure(failure)
            else:
                response.success()
        if first is not None:
            events.request.fire(request_type="CHAT", name="chat: first event",
                                response_time=(first - started) * 1000, response_length=0,
                                exception=None, context={})
        fire("chat: complete", started, Exception(failure) if failure else None, kind="CHAT")
