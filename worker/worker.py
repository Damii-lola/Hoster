"""
Hoster worker — runs inside a GitHub Actions job.

What it does, in order:
  1. Registers itself in the `workers` table.
  2. Downloads + loads a quantized Qwen2.5-7B-Instruct GGUF model.
  3. Loops: reclaims any stale/abandoned jobs, claims one pending job,
     runs inference, writes the result back.
  4. Stops claiming new jobs a safety margin before the runner's time
     limit and shuts down cleanly.

Every database call opens its own short-lived connection rather than
holding one open for the worker's whole lifetime. This matters because
TiDB (like most managed MySQL-compatible services) closes connections
that sit idle too long — and the ~45 minutes a cold model download can
take is far longer than that idle timeout. One connection held open
across the download would already be dead by the time it's used again.

Required environment variables (set as GitHub Actions secrets):
    TIDB_CONNECTION_STRING   (or the individual TIDB_HOST/PORT/USER/PASSWORD/DATABASE — see db.py)

Optional:
    WORKER_ID              defaults to "gha-" + a short random id
    MAX_RUNTIME_SECONDS    defaults to 17700 (4h55m) — keep under GitHub's 6h cap
    LEASE_SECONDS          defaults to 600 (10 min) — how long a claimed job
                           is considered "alive" before another worker may
                           reclaim it
"""

import os
import sys
import time
import uuid
import signal
import traceback

os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")  # much faster model download

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection  # noqa: E402

MODEL_REPO = os.environ.get("MODEL_REPO", "bartowski/Qwen2.5-7B-Instruct-GGUF")
MODEL_FILE = os.environ.get("MODEL_FILE", "Qwen2.5-7B-Instruct-Q4_K_M.gguf")
MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct")

WORKER_ID = os.environ.get("WORKER_ID", f"gha-{uuid.uuid4().hex[:8]}")
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", 17700))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", 600))
SHUTDOWN_MARGIN_SECONDS = 300  # stop claiming new jobs this long before the hard cutoff

_stop_requested = False


def _handle_sigterm(signum, frame):
    global _stop_requested
    _stop_requested = True


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def _run(fn, *args, retries=2):
    """Open a fresh connection, run fn(conn, *args), close it. Retries
    once on a dropped-connection error, since that's the one failure
    mode we expect from an idle/long-lived link, not from bad queries."""
    last_exc = None
    for attempt in range(retries + 1):
        conn = get_connection()
        try:
            return fn(conn, *args)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"[{WORKER_ID}] db call failed (attempt {attempt + 1}): {exc}", flush=True)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    raise last_exc


def download_model():
    from huggingface_hub import hf_hub_download

    print(f"[{WORKER_ID}] downloading {MODEL_FILE} from {MODEL_REPO} ...", flush=True)
    path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE)
    print(f"[{WORKER_ID}] model ready at {path}", flush=True)
    return path


def load_model(path):
    from llama_cpp import Llama

    cpu_count = os.cpu_count() or 4
    print(f"[{WORKER_ID}] loading model with {cpu_count} threads ...", flush=True)
    llm = Llama(
        model_path=path,
        n_ctx=4096,
        n_threads=cpu_count,
        verbose=False,
    )
    print(f"[{WORKER_ID}] model loaded", flush=True)
    return llm


def _register_worker(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workers (worker_id, available_models, status)
            VALUES (%s, %s, 'starting')
            ON DUPLICATE KEY UPDATE
                available_models = VALUES(available_models),
                status = 'starting',
                jobs_completed = 0,
                jobs_failed = 0,
                started_at = CURRENT_TIMESTAMP
            """,
            (WORKER_ID, MODEL_NAME),
        )


def _set_worker_status(conn, status, current_job_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workers SET status = %s, current_job_id = %s, "
            "last_heartbeat = CURRENT_TIMESTAMP WHERE worker_id = %s",
            (status, current_job_id, WORKER_ID),
        )


def _heartbeat(conn):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workers SET last_heartbeat = CURRENT_TIMESTAMP WHERE worker_id = %s",
            (WORKER_ID,),
        )


def _reclaim_stale_jobs(conn):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'pending', claimed_by = NULL "
            "WHERE status = 'processing' AND lease_expires_at < NOW() "
            "AND attempt_count < max_attempts"
        )
        cur.execute(
            "UPDATE jobs SET status = 'failed', "
            "error_message = 'exceeded max_attempts after worker loss' "
            "WHERE status = 'processing' AND lease_expires_at < NOW() "
            "AND attempt_count >= max_attempts"
        )


def _claim_one_job(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM jobs WHERE status = 'pending' AND model_name = %s "
            "ORDER BY created_at ASC LIMIT 5",
            (MODEL_NAME,),
        )
        candidates = [r["id"] for r in cur.fetchall()]

    for job_id in candidates:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'processing', claimed_by = %s, "
                "attempt_count = attempt_count + 1, "
                "lease_expires_at = NOW() + INTERVAL %s SECOND "
                "WHERE id = %s AND status = 'pending'",
                (WORKER_ID, LEASE_SECONDS, job_id),
            )
            if cur.rowcount == 1:
                return job_id
    return None


def _fetch_job_input(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT input FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return row["input"] if row else None


def run_inference(llm, prompt_text):
    messages = [{"role": "user", "content": prompt_text}]
    out = llm.create_chat_completion(messages=messages, max_tokens=512, temperature=0.7)
    return out["choices"][0]["message"]["content"]


def _complete_job(conn, job_id, result_text):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'done', result = %s WHERE id = %s",
            (result_text, job_id),
        )


def _fail_job(conn, job_id, error_text):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_count, max_attempts FROM jobs WHERE id = %s", (job_id,)
        )
        row = cur.fetchone()
        if row and row["attempt_count"] < row["max_attempts"]:
            cur.execute(
                "UPDATE jobs SET status = 'pending', claimed_by = NULL, "
                "error_message = %s WHERE id = %s",
                (error_text, job_id),
            )
        else:
            cur.execute(
                "UPDATE jobs SET status = 'failed', error_message = %s WHERE id = %s",
                (error_text, job_id),
            )


def _bump_counter(conn, column):
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE workers SET {column} = {column} + 1 WHERE worker_id = %s",
            (WORKER_ID,),
        )


def main():
    start_time = time.time()
    _run(_register_worker)

    # No DB connection is held open across this step on purpose — it can
    # take a long time, and a connection left idle that long gets dropped.
    model_path = download_model()
    llm = load_model(model_path)

    _run(_set_worker_status, "ready", None)
    print(f"[{WORKER_ID}] worker ready, entering poll loop", flush=True)

    poll_interval = 2
    max_poll_interval = 30

    try:
        while not _stop_requested:
            elapsed = time.time() - start_time
            if elapsed > MAX_RUNTIME_SECONDS - SHUTDOWN_MARGIN_SECONDS:
                print(f"[{WORKER_ID}] approaching time budget, shutting down", flush=True)
                break

            _run(_heartbeat)
            _run(_reclaim_stale_jobs)

            job_id = _run(_claim_one_job)
            if job_id is None:
                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 2, max_poll_interval)
                continue

            poll_interval = 2  # reset backoff — we found work
            _run(_set_worker_status, "busy", job_id)
            print(f"[{WORKER_ID}] claimed job {job_id}", flush=True)

            try:
                prompt_text = _run(_fetch_job_input, job_id)
                result_text = run_inference(llm, prompt_text)
                _run(_complete_job, job_id, result_text)
                print(f"[{WORKER_ID}] completed job {job_id}", flush=True)
                _run(_bump_counter, "jobs_completed")
            except Exception as exc:  # noqa: BLE001
                print(f"[{WORKER_ID}] job {job_id} failed: {exc}", flush=True)
                traceback.print_exc()
                _run(_fail_job, job_id, str(exc)[:500])
                _run(_bump_counter, "jobs_failed")
            finally:
                _run(_set_worker_status, "ready", None)
    finally:
        try:
            _run(_set_worker_status, "offline", None)
        except Exception:  # noqa: BLE001
            pass
        print(f"[{WORKER_ID}] shut down cleanly", flush=True)


if __name__ == "__main__":
    main()
