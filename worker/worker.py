"""
Hoster worker — runs inside a GitHub Actions job.

What it does, in order:
  1. Registers itself in the `workers` table and logs a `worker_runs` row.
  2. Downloads + loads a quantized Qwen2.5-7B-Instruct GGUF model,
     timing both steps and detecting whether the model came from cache.
  3. Loops: reclaims any stale/abandoned jobs, claims one pending job,
     runs inference while recording timing/resource metrics, writes the
     result + those metrics back.
  4. Stops claiming new jobs a safety margin before the runner's time
     limit, finalizes its `worker_runs` row, and shuts down cleanly.

Every database call opens its own short-lived connection rather than
holding one open for the worker's whole lifetime — TiDB closes
connections that sit idle too long, and a cold model download can take
far longer than that idle timeout.

Required environment variables (set as GitHub Actions secrets):
    TIDB_CONNECTION_STRING   (or the individual TIDB_HOST/PORT/USER/PASSWORD/DATABASE — see db.py)

Optional:
    WORKER_ID                      defaults to "gha-" + a short random id
    MAX_RUNTIME_SECONDS            defaults to 17700 (4h55m) — keep under GitHub's 6h cap
    LEASE_SECONDS                  defaults to 90 — how long a claimed job is
                                    considered "alive" without a renewal
    LEASE_RENEWAL_INTERVAL_SECONDS defaults to 30 — how often an in-progress
                                    job's lease is pushed forward
"""

import os
import sys
import time
import uuid
import signal
import resource
import threading
import traceback

os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")  # much faster model download

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection  # noqa: E402

MODEL_REPO = os.environ.get("MODEL_REPO", "bartowski/Qwen2.5-7B-Instruct-GGUF")
MODEL_FILE = os.environ.get("MODEL_FILE", "Qwen2.5-7B-Instruct-Q4_K_M.gguf")
MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct")
DEFAULT_MAX_TOKENS = int(os.environ.get("DEFAULT_MAX_TOKENS", 300))

WORKER_ID = os.environ.get("WORKER_ID", f"gha-{uuid.uuid4().hex[:8]}")
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", 17700))

# How long a claimed job is considered "alive" without a renewal before
# another worker may reclaim it. Short on purpose: as long as this worker
# is actually still processing, it renews the lease well before this
# expires, so a genuinely slow job is never wrongly reclaimed. If the
# worker dies, renewals stop and the job becomes reclaimable quickly.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", 90))
LEASE_RENEWAL_INTERVAL_SECONDS = int(os.environ.get("LEASE_RENEWAL_INTERVAL_SECONDS", 30))

# Same idea as the job lease above, but for the worker's own row in
# `workers`: a persistent background thread (started once, for the
# worker's whole lifetime) heartbeats every WORKER_HEARTBEAT_INTERVAL_SECONDS
# regardless of idle/busy state. Any worker whose heartbeat goes silent
# for WORKER_STALE_SECONDS gets marked offline by whichever worker next
# runs the stale-check — cleaning up rows left behind by a real crash
# (which skips normal shutdown) without ever misclassifying a worker
# that's just busy on a long job.
WORKER_HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", 20))
WORKER_STALE_SECONDS = int(os.environ.get("WORKER_STALE_SECONDS", 90))

# Test-only hook (Phase 2B): if set, this worker hard-kills its own
# process this many seconds after becoming ready — simulating a real
# crash from inside the process, rather than relying on GitHub's
# cancel-workflow button, which cancels an entire matrix run at once
# and can't target one job independently. Unset in production.
SIMULATE_CRASH_AFTER_SECONDS = os.environ.get("SIMULATE_CRASH_AFTER_SECONDS", "").strip()

SHUTDOWN_MARGIN_SECONDS = 300  # stop claiming new jobs this long before the hard cutoff
CPU_COUNT = os.cpu_count() or 4

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
    from huggingface_hub import hf_hub_download, try_to_load_from_cache

    cached_path = try_to_load_from_cache(MODEL_REPO, MODEL_FILE)
    cache_hit = isinstance(cached_path, str)

    print(f"[{WORKER_ID}] downloading {MODEL_FILE} from {MODEL_REPO} ...", flush=True)
    t0 = time.time()
    path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE)
    download_seconds = time.time() - t0
    print(
        f"[{WORKER_ID}] model ready at {path} "
        f"({download_seconds:.1f}s, cache_hit={cache_hit})",
        flush=True,
    )
    return path, download_seconds, cache_hit


def load_model(path):
    from llama_cpp import Llama

    print(f"[{WORKER_ID}] loading model with {CPU_COUNT} threads ...", flush=True)
    t0 = time.time()
    llm = Llama(
        model_path=path,
        n_ctx=4096,
        n_threads=CPU_COUNT,
        n_threads_batch=CPU_COUNT,
        verbose=False,
    )
    load_seconds = time.time() - t0
    print(f"[{WORKER_ID}] model loaded ({load_seconds:.1f}s)", flush=True)
    return llm, load_seconds


def _register_worker(conn):
    # GITHUB_RUN_ID is a built-in Actions variable identifying this
    # specific run. Phase 2C's orchestrator stores it so it can later
    # cancel this exact worker's run without touching any other.
    github_run_id = os.environ.get("GITHUB_RUN_ID")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workers (worker_id, available_models, status, github_run_id)
            VALUES (%s, %s, 'starting', %s)
            ON DUPLICATE KEY UPDATE
                available_models = VALUES(available_models),
                status = 'starting',
                jobs_completed = 0,
                jobs_failed = 0,
                started_at = CURRENT_TIMESTAMP,
                github_run_id = VALUES(github_run_id)
            """,
            (WORKER_ID, MODEL_NAME, github_run_id),
        )


def _start_worker_run(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_runs (worker_id, model_name, model_version) "
            "VALUES (%s, %s, %s)",
            (WORKER_ID, MODEL_NAME, MODEL_FILE),
        )
        return cur.lastrowid


def _update_worker_run_startup(conn, run_id, download_seconds, load_seconds, cache_hit):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE worker_runs SET download_seconds = %s, load_seconds = %s, "
            "startup_seconds = %s, cache_hit = %s WHERE id = %s",
            (download_seconds, load_seconds, download_seconds + load_seconds, cache_hit, run_id),
        )


def _finalize_worker_run(conn, run_id, jobs_completed, jobs_failed):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE worker_runs SET ended_at = NOW(), jobs_completed = %s, "
            "jobs_failed = %s WHERE id = %s",
            (jobs_completed, jobs_failed, run_id),
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


def _start_worker_heartbeat():
    """Runs for this worker's entire lifetime, independent of whether
    it's idle or busy on a job. Without this, last_heartbeat only moves
    between jobs — so a worker genuinely busy on one long task looks
    exactly like a worker that silently died. This thread is what makes
    'no heartbeat in WORKER_STALE_SECONDS' a reliable crash signal
    instead of a false alarm on slow (but healthy) work."""
    stop_event = threading.Event()

    def _loop():
        while not stop_event.wait(WORKER_HEARTBEAT_INTERVAL_SECONDS):
            try:
                _run(_heartbeat, retries=1)
            except Exception as exc:  # noqa: BLE001
                print(f"[{WORKER_ID}] worker heartbeat failed: {exc}", flush=True)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop_event, thread


def _mark_stale_workers_offline(conn):
    """Any worker (this one or another) whose heartbeat has gone silent
    for WORKER_STALE_SECONDS is marked offline. Safe to call from any
    worker — it's just a conditional UPDATE, and whichever worker
    happens to run it first wins; no coordination needed."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workers SET status = 'offline' "
            "WHERE status != 'offline' AND last_heartbeat < NOW() - INTERVAL %s SECOND",
            (WORKER_STALE_SECONDS,),
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
                "lease_expires_at = NOW() + INTERVAL %s SECOND, "
                "claimed_at = NOW() "
                "WHERE id = %s AND status = 'pending'",
                (WORKER_ID, LEASE_SECONDS, job_id),
            )
            if cur.rowcount == 1:
                return job_id
    return None


def _renew_lease(conn, job_id):
    """Push the job's deadline forward. Only takes effect while the job
    is still 'processing' under this worker — if it already finished,
    failed, or got reclaimed by someone else, this is a harmless no-op."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET lease_expires_at = NOW() + INTERVAL %s SECOND "
            "WHERE id = %s AND status = 'processing' AND claimed_by = %s",
            (LEASE_SECONDS, job_id, WORKER_ID),
        )


def _start_lease_renewal(job_id):
    """Runs in a background thread for as long as this job is being
    processed, renewing its lease periodically. If the main thread dies
    (crash, kill, cancelled workflow), this thread dies with it and
    renewals simply stop — which is exactly what lets another worker
    detect the failure and reclaim the job quickly."""
    stop_event = threading.Event()

    def _loop():
        while not stop_event.wait(LEASE_RENEWAL_INTERVAL_SECONDS):
            try:
                _run(_renew_lease, job_id, retries=1)
            except Exception as exc:  # noqa: BLE001
                print(f"[{WORKER_ID}] lease renewal failed: {exc}", flush=True)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop_event, thread


def _fetch_job_details(conn, job_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT input, created_at, claimed_at, max_tokens FROM jobs WHERE id = %s",
            (job_id,),
        )
        return cur.fetchone()


def run_inference(llm, prompt_text, max_tokens):
    """Runs one inference call and measures it. peak_rss is the whole
    process's peak resident memory at this moment (dominated by the
    loaded model weights) — it's not isolated to this one job, since a
    long-lived worker process can't cleanly separate memory use between
    requests. Treat it as a per-worker ceiling, not a per-job cost."""
    messages = [{"role": "user", "content": prompt_text}]

    wall_start = time.time()
    cpu_start = time.process_time()
    out = llm.create_chat_completion(messages=messages, max_tokens=max_tokens, temperature=0.7)
    wall_elapsed = time.time() - wall_start
    cpu_elapsed = time.process_time() - cpu_start

    result_text = out["choices"][0]["message"]["content"]
    cpu_utilization_pct = (
        min(100.0, (cpu_elapsed / wall_elapsed / CPU_COUNT) * 100) if wall_elapsed > 0 else 0.0
    )
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB -> MB on Linux

    metrics = dict(
        inference_seconds=round(wall_elapsed, 3),
        cpu_seconds=round(cpu_elapsed, 3),
        cpu_utilization_pct=round(cpu_utilization_pct, 1),
        peak_rss_mb=round(peak_rss_mb, 1),
        input_chars=len(prompt_text),
        output_chars=len(result_text),
    )
    return result_text, metrics


def _complete_job(conn, job_id, result_text, queue_wait_seconds, metrics):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'done', result = %s, completed_at = NOW(), "
            "total_seconds = TIMESTAMPDIFF(SECOND, created_at, NOW()), "
            "queue_wait_seconds = %s, inference_seconds = %s, cpu_seconds = %s, "
            "cpu_utilization_pct = %s, peak_rss_mb = %s, input_chars = %s, "
            "output_chars = %s, model_version = %s "
            "WHERE id = %s",
            (
                result_text,
                queue_wait_seconds,
                metrics["inference_seconds"],
                metrics["cpu_seconds"],
                metrics["cpu_utilization_pct"],
                metrics["peak_rss_mb"],
                metrics["input_chars"],
                metrics["output_chars"],
                MODEL_FILE,
                job_id,
            ),
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
    run_id = _run(_start_worker_run)

    # No DB connection is held open across this step on purpose — it can
    # take a long time, and a connection left idle that long gets dropped.
    model_path, download_seconds, cache_hit = download_model()
    llm, load_seconds = load_model(model_path)
    _run(_update_worker_run_startup, run_id, download_seconds, load_seconds, cache_hit)

    _run(_set_worker_status, "ready", None)
    print(f"[{WORKER_ID}] worker ready, entering poll loop", flush=True)

    stop_worker_heartbeat, worker_heartbeat_thread = _start_worker_heartbeat()

    if SIMULATE_CRASH_AFTER_SECONDS:
        delay = float(SIMULATE_CRASH_AFTER_SECONDS)

        def _simulate_crash():
            time.sleep(delay)
            print(f"[{WORKER_ID}] SIMULATED CRASH NOW (test hook) — hard-killing process, "
                  f"no cleanup, no graceful shutdown", flush=True)
            os._exit(137)  # bypasses all finally/except blocks — a real crash, not an exit

        threading.Thread(target=_simulate_crash, daemon=True).start()
        print(f"[{WORKER_ID}] test hook armed: will simulate a crash in {delay}s", flush=True)

    poll_interval = 1
    max_poll_interval = 8  # was 30 — Phase 2A metrics showed jobs landing mid-sleep cost 20s+ of pure queue wait
    local_completed = 0
    local_failed = 0

    try:
        while not _stop_requested:
            elapsed = time.time() - start_time
            if elapsed > MAX_RUNTIME_SECONDS - SHUTDOWN_MARGIN_SECONDS:
                print(f"[{WORKER_ID}] approaching time budget, shutting down", flush=True)
                break

            _run(_reclaim_stale_jobs)
            _run(_mark_stale_workers_offline)

            job_id = _run(_claim_one_job)
            if job_id is None:
                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, max_poll_interval)
                continue

            poll_interval = 1  # reset backoff — we found work
            _run(_set_worker_status, "busy", job_id)
            print(f"[{WORKER_ID}] claimed job {job_id}", flush=True)

            stop_renewal, renewal_thread = _start_lease_renewal(job_id)
            try:
                details = _run(_fetch_job_details, job_id)
                queue_wait_seconds = None
                if details["claimed_at"] and details["created_at"]:
                    queue_wait_seconds = round(
                        (details["claimed_at"] - details["created_at"]).total_seconds(), 3
                    )
                max_tokens = details["max_tokens"] or DEFAULT_MAX_TOKENS

                result_text, metrics = run_inference(llm, details["input"], max_tokens)
                _run(_complete_job, job_id, result_text, queue_wait_seconds, metrics)
                print(
                    f"[{WORKER_ID}] completed job {job_id} "
                    f"(queue_wait={queue_wait_seconds}s, inference={metrics['inference_seconds']}s, "
                    f"cpu_util={metrics['cpu_utilization_pct']}%)",
                    flush=True,
                )
                _run(_bump_counter, "jobs_completed")
                local_completed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[{WORKER_ID}] job {job_id} failed: {exc}", flush=True)
                traceback.print_exc()
                _run(_fail_job, job_id, str(exc)[:500])
                _run(_bump_counter, "jobs_failed")
                local_failed += 1
            finally:
                stop_renewal.set()
                renewal_thread.join(timeout=5)
                _run(_set_worker_status, "ready", None)
    finally:
        stop_worker_heartbeat.set()
        worker_heartbeat_thread.join(timeout=5)
        try:
            _run(_finalize_worker_run, run_id, local_completed, local_failed)
        except Exception:  # noqa: BLE001
            pass
        try:
            _run(_set_worker_status, "offline", None)
        except Exception:  # noqa: BLE001
            pass
        print(f"[{WORKER_ID}] shut down cleanly", flush=True)


if __name__ == "__main__":
    main()
