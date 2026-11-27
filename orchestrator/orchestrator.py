"""
Hoster orchestrator (Phase 2C).

Runs on its own schedule (orchestrator.yml). Each cycle, for every
model that currently has pending jobs or currently-alive auto-scaled
workers, it answers the questions this phase exists to answer:

  - Is the queue growing?        -> pending_jobs, logged every cycle (compare across rows yourself, or see /orchestrator/log)
  - How many workers are needed? -> desired_workers, from a simple, tunable formula
  - Is a worker actually ready?  -> only status IN ('ready','busy') with a fresh
                                    heartbeat counts as real capacity; 'starting'
                                    counts as committed-but-not-yet-serving
  - Which models should they load? -> MODEL_REGISTRY below, keyed by model_name
  - When should they start/stop?   -> this cycle, if committed count != desired
  - Which worker receives the next job? -> deliberately NOT decided here — jobs
        are claimed by atomic SQL UPDATE in worker.py itself (proven race-free
        and fair in Phase 2B). Re-implementing assignment here would duplicate
        that guarantee with strictly more risk, not less.

This script ONLY ever manages workers whose worker_id starts with
AUTO_WORKER_PREFIX. It never touches the standing baseline worker
(gha-worker-1) or manual/test workers — that's a deliberate safety
boundary, not an oversight.

Required env: TIDB_CONNECTION_STRING (or the individual TIDB_* vars),
GITHUB_TOKEN, GITHUB_REPOSITORY (both auto-provided inside Actions).
"""

import os
import sys
import math
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection  # noqa: E402

# Add an entry here whenever a new model should be eligible for
# orchestration. This is the one piece of manual config the system
# needs — everything else is derived from live data.
MODEL_REGISTRY = {
    "Qwen/Qwen2.5-7B-Instruct": dict(
        model_repo="bartowski/Qwen2.5-7B-Instruct-GGUF",
        model_file="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
    ),
    "Qwen/Qwen2.5-3B-Instruct": dict(
        model_repo="bartowski/Qwen2.5-3B-Instruct-GGUF",
        model_file="Qwen2.5-3B-Instruct-Q4_K_M.gguf",
    ),
}

AUTO_WORKER_PREFIX = "gha-auto-"
JOBS_PER_WORKER_TARGET = int(os.environ.get("JOBS_PER_WORKER_TARGET", 3))
MAX_WORKERS_PER_MODEL = int(os.environ.get("MAX_WORKERS_PER_MODEL", 3))
WORKER_STALE_SECONDS = int(os.environ.get("WORKER_STALE_SECONDS", 90))
ORCHESTRATED_MAX_RUNTIME_SECONDS = os.environ.get("ORCHESTRATED_MAX_RUNTIME_SECONDS", "1500")

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]  # "owner/repo"
API_BASE = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def fetch_relevant_models(conn):
    """Any model with pending work OR any currently-alive auto-worker —
    the union is what needs a decision this cycle."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT model_name FROM jobs WHERE status = 'pending'"
        )
        pending_models = {r["model_name"] for r in cur.fetchall()}

        cur.execute(
            "SELECT DISTINCT available_models AS model_name FROM workers "
            "WHERE worker_id LIKE %s AND status IN ('starting','ready','busy') "
            "AND last_heartbeat > NOW() - INTERVAL %s SECOND",
            (f"{AUTO_WORKER_PREFIX}%", WORKER_STALE_SECONDS),
        )
        active_models = {r["model_name"] for r in cur.fetchall()}

    return pending_models | active_models


def fetch_model_state(conn, model_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending' AND model_name = %s",
            (model_name,),
        )
        pending_jobs = cur.fetchone()["n"]

        cur.execute(
            "SELECT worker_id, status, current_job_id, github_run_id FROM workers "
            "WHERE worker_id LIKE %s AND available_models = %s "
            "AND status IN ('starting','ready','busy') "
            "AND last_heartbeat > NOW() - INTERVAL %s SECOND",
            (f"{AUTO_WORKER_PREFIX}%", model_name, WORKER_STALE_SECONDS),
        )
        committed_workers = cur.fetchall()

    return pending_jobs, committed_workers


def compute_desired(pending_jobs):
    if pending_jobs == 0:
        return 0
    return min(MAX_WORKERS_PER_MODEL, max(1, math.ceil(pending_jobs / JOBS_PER_WORKER_TARGET)))


def launch_worker(model_name):
    config = MODEL_REGISTRY[model_name]
    worker_id = f"{AUTO_WORKER_PREFIX}{uuid.uuid4().hex[:10]}"
    resp = requests.post(
        f"{API_BASE}/actions/workflows/orchestrated-worker.yml/dispatches",
        headers=GH_HEADERS,
        json={
            "ref": "main",
            "inputs": {
                "worker_id": worker_id,
                "model_repo": config["model_repo"],
                "model_file": config["model_file"],
                "model_name": model_name,
                "max_runtime_seconds": ORCHESTRATED_MAX_RUNTIME_SECONDS,
            },
        },
        timeout=15,
    )
    ok = resp.status_code == 204
    print(f"  launch {worker_id}: HTTP {resp.status_code} {'OK' if ok else resp.text[:200]}")
    return worker_id if ok else None


def stop_worker(worker):
    run_id = worker["github_run_id"]
    if not run_id:
        print(f"  skip stopping {worker['worker_id']}: no github_run_id on record")
        return False
    resp = requests.post(
        f"{API_BASE}/actions/runs/{run_id}/cancel",
        headers=GH_HEADERS,
        timeout=15,
    )
    ok = resp.status_code in (202, 409)  # 409 = already completing/cancelled, treat as fine
    print(f"  stop {worker['worker_id']} (run {run_id}): HTTP {resp.status_code} {'OK' if ok else resp.text[:200]}")
    return ok


def log_decision(conn, model_name, pending_jobs, committed_count, desired, action, launched, stopped, detail):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orchestrator_log "
            "(model_name, pending_jobs, committed_workers, desired_workers, "
            "action, workers_launched, workers_stopped, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (model_name, pending_jobs, committed_count, desired, action, launched, stopped, detail),
        )


def main():
    conn = get_connection()
    try:
        models = fetch_relevant_models(conn)
    finally:
        conn.close()

    if not models:
        print("No models with pending jobs or active auto-workers this cycle — nothing to do.")
        return

    for model_name in sorted(models):
        if model_name not in MODEL_REGISTRY:
            print(f"[{model_name}] has demand but no MODEL_REGISTRY entry — add one to orchestrate it. Skipping.")
            continue

        conn = get_connection()
        try:
            pending_jobs, committed_workers = fetch_model_state(conn, model_name)
        finally:
            conn.close()

        committed_count = len(committed_workers)
        desired = compute_desired(pending_jobs)

        print(f"[{model_name}] pending={pending_jobs} committed={committed_count} desired={desired}")

        launched, stopped = 0, 0
        action = "no_change"
        detail = ""

        if committed_count < desired:
            action = "scale_up"
            to_launch = desired - committed_count
            detail = f"launching {to_launch} worker(s)"
            print(f"  {detail}")
            for _ in range(to_launch):
                if launch_worker(model_name):
                    launched += 1

        elif committed_count > desired:
            action = "scale_down"
            to_stop = committed_count - desired
            idle_candidates = [w for w in committed_workers if w["status"] == "ready" and w["current_job_id"] is None]
            busy_candidates = [w for w in committed_workers if w not in idle_candidates]
            # Prefer stopping idle workers first; only touch busy ones if we
            # must, and the existing lease/reclaim system already guarantees
            # whatever job they're holding survives this safely either way.
            candidates = (idle_candidates + busy_candidates)[:to_stop]
            detail = f"stopping {len(candidates)} worker(s) ({len(idle_candidates)} idle available)"
            print(f"  {detail}")
            for w in candidates:
                if stop_worker(w):
                    stopped += 1
        else:
            detail = "at target, no action needed"

        conn = get_connection()
        try:
            log_decision(conn, model_name, pending_jobs, committed_count, desired, action, launched, stopped, detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
