"""Run the load test at several user counts and collect the numbers for the report.

    backend/.venv/Scripts/python loadtest/run_sweep.py --users 5 20 50 --seconds 60 --label baseline

For each user count it starts Locust (headless), samples the queue and the containers' CPU while
it runs, waits for the queue to drain, and writes loadtest/results/<label>-summary.md/.json.
The stack must already be up in fake-LLM mode (see loadtest/README.md); the Locust file refuses
to run otherwise.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
HOST = "http://127.0.0.1:8000"
TOKEN = os.getenv("LOADTEST_METRICS_TOKEN", "loadtest-token")
CONTAINERS = ("docsum-api", "docsum-worker", "docsum-db", "docsum-redis")
ROWS = {
    "GET /documents": "GET /documents",
    "GET /jobs/{id}": "GET /jobs/{id}",
    "POST /documents/upload/stream (queue)": "upload request (202)",
    "job: upload -> completed": "job: upload -> done",
    "chat: first event": "chat: first event",
    "chat: complete": "chat: complete",
    "Aggregated": "ALL requests",
}


def queue_numbers() -> dict:
    body = requests.get(f"{HOST}/metrics", timeout=5, headers={"Authorization": f"Bearer {TOKEN}"}).text
    numbers = {"queue_depth": 0, "pending": 0, "processing": 0}
    for line in body.splitlines():
        if line.startswith("documind_queue_depth "):
            numbers["queue_depth"] = int(float(line.split()[-1]))
        elif line.startswith('documind_jobs{mode="queue",state="pending"}'):
            numbers["pending"] = int(float(line.split()[-1]))
        elif line.startswith('documind_jobs{mode="queue",state="processing"}'):
            numbers["processing"] = int(float(line.split()[-1]))
    return numbers


def wait_for_drain(limit: int = 240) -> float:
    """Seconds until no job is waiting or running (so the next run starts from an empty system)."""
    started = time.time()
    while time.time() - started < limit:
        n = queue_numbers()
        if n["queue_depth"] == 0 and n["pending"] == 0 and n["processing"] == 0:
            break
        time.sleep(2)
    return round(time.time() - started, 1)


def sample_containers(stop: threading.Event, out: dict) -> None:
    """CPU % of each container every few seconds; keeps the average and the peak."""
    seen = {name: [] for name in CONTAINERS}
    while not stop.is_set():
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}", *CONTAINERS],
            capture_output=True, text=True)
        for line in result.stdout.splitlines():
            name, _, cpu = line.partition(" ")
            if name in seen and cpu.endswith("%"):
                seen[name].append(float(cpu[:-1]))
    out.update({name: {"avg": round(sum(v) / len(v), 1), "peak": round(max(v), 1)}
                for name, v in seen.items() if v})


def run_one(users: int, seconds: int, label: str) -> dict:
    prefix = RESULTS / f"{label}-u{users}"
    samples = RESULTS / f"{label}-u{users}_samples.csv"
    env = {**os.environ, "LOADTEST_SAMPLES_FILE": str(samples)}
    cpu: dict = {}
    stop = threading.Event()
    sampler = threading.Thread(target=sample_containers, args=(stop, cpu), daemon=True)
    sampler.start()
    command = [sys.executable, "-m", "locust", "-f", str(HERE / "locustfile.py"), "--headless",
               "-u", str(users), "-r", str(max(1, users // 5)), "-t", f"{seconds}s",
               "--host", HOST, "--csv", str(prefix), "--only-summary", "--csv-full-history"]
    process = subprocess.run(command, env=env, capture_output=True, text=True)
    stop.set()
    sampler.join(timeout=15)
    if process.returncode != 0:
        print(process.stdout[-1500:], process.stderr[-1500:])
        raise SystemExit(f"Locust exited with {process.returncode} (refused or stopped); see above.")

    stats = {}
    with open(f"{prefix}_stats.csv", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["Name"] in ROWS:
                stats[row["Name"]] = {
                    "count": int(row["Request Count"]), "fails": int(row["Failure Count"]),
                    "rps": float(row["Requests/s"]), "p50": float(row["50%"]),
                    "p95": float(row["95%"]), "p99": float(row["99%"]),
                }
    max_depth, max_pending, end_backlog = 0, 0, 0
    if samples.exists():
        with open(samples, newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(handle) if r["queue_depth"] != ""]
        if rows:
            max_depth = max(int(float(r["queue_depth"])) for r in rows)
            max_pending = max(int(float(r["pending"])) + int(float(r["processing"])) for r in rows)
            last = rows[-1]
            end_backlog = int(float(last["pending"])) + int(float(last["processing"]))
    drain = wait_for_drain()
    return {"users": users, "seconds": seconds, "stats": stats, "cpu": cpu,
            "max_queue_depth": max_depth, "max_jobs_waiting_or_running": max_pending,
            "jobs_unfinished_at_end": end_backlog, "drain_seconds": drain}


def write_summary(label: str, results: list[dict]) -> None:
    (RESULTS / f"{label}-summary.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    lines = [f"# Load test: {label}", ""]
    for name, title in ROWS.items():
        lines += [f"## {title}", "", "| users | requests | failed | req/s | p50 ms | p95 ms | p99 ms |",
                  "|---|---|---|---|---|---|---|"]
        for r in results:
            s = r["stats"].get(name)
            if s:
                lines.append(f"| {r['users']} | {s['count']} | {s['fails']} | {s['rps']:.1f} | "
                             f"{s['p50']:.0f} | {s['p95']:.0f} | {s['p99']:.0f} |")
        lines.append("")
    lines += ["## Queue and containers", "",
              "| users | max queue depth | max jobs waiting/running | unfinished at end | drain s | "
              "CPU % avg/peak: api | worker | db | redis |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        cpu = {k: f"{v['avg']:.0f}/{v['peak']:.0f}" for k, v in r["cpu"].items()}
        lines.append(f"| {r['users']} | {r['max_queue_depth']} | {r['max_jobs_waiting_or_running']} | "
                     f"{r['jobs_unfinished_at_end']} | {r['drain_seconds']} | "
                     f"{cpu.get('docsum-api', '-')} | {cpu.get('docsum-worker', '-')} | "
                     f"{cpu.get('docsum-db', '-')} | {cpu.get('docsum-redis', '-')} |")
    (RESULTS / f"{label}-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, nargs="+", default=[5, 20, 50])
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--label", default="run")
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)
    print(f"Waiting for an idle system ({wait_for_drain()} s)...")
    results = []
    for count in args.users:
        print(f"Running {count} users for {args.seconds} s...")
        results.append(run_one(count, args.seconds, args.label))
    write_summary(args.label, results)
