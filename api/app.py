"""
Hoster public API (deploy this on Render).

Endpoints:
    POST /run-ai     { "model_name": "...", "input": "...", "max_tokens": 300 }  -> { "job_id": "..." }
    GET  /result/<job_id>                                     -> { "status": ..., "result": ..., metrics... }
    GET  /health                                               -> { "ok": true, "workers_ready": N }
    GET  /metrics                                              -> aggregate benchmark stats (Phase 2A)

This service never talks to GitHub or runs any model itself. It only
writes/reads rows in TiDB. GitHub Actions workers pick the jobs up on
their own schedule.

max_tokens is the main speed lever available per request: it caps how
much the model is allowed to generate. Less generation is the single
biggest lever over response time on CPU — bigger than any of the
infra-side optimizations — so callers who want a fast reply should pass
a small value (e.g. 60-100) rather than relying on the server default.
"""

import os
import sys
import uuid

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection  # noqa: E402

app = Flask(__name__)

MAX_INPUT_CHARS = 8000  # keep a single job from eating a worker's whole lifetime
MAX_TOKENS_CEILING = 2048  # hard cap regardless of what a caller asks for
DEFAULT_MAX_TOKENS = 300


@app.post("/run-ai")
def run_ai():
    body = request.get_json(silent=True) or {}
    model_name = body.get("model_name")
    input_text = body.get("input")
    max_tokens = body.get("max_tokens", DEFAULT_MAX_TOKENS)

    if not model_name or not input_text:
        return jsonify(error="model_name and input are required"), 400
    if len(input_text) > MAX_INPUT_CHARS:
        return jsonify(error=f"input too long (max {MAX_INPUT_CHARS} chars)"), 400
    if not isinstance(max_tokens, int) or not (1 <= max_tokens <= MAX_TOKENS_CEILING):
        return jsonify(error=f"max_tokens must be an integer from 1 to {MAX_TOKENS_CEILING}"), 400

    job_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, status, model_name, input, max_tokens) "
                "VALUES (%s, 'pending', %s, %s, %s)",
                (job_id, model_name, input_text, max_tokens),
            )
    finally:
        conn.close()

    return jsonify(job_id=job_id, status="pending"), 202


@app.get("/result/<job_id>")
def get_result(job_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result, error_message, created_at, updated_at, "
                "queue_wait_seconds, inference_seconds, total_seconds, "
                "cpu_seconds, cpu_utilization_pct, peak_rss_mb, "
                "input_chars, output_chars, model_version, max_tokens "
                "FROM jobs WHERE id = %s",
                (job_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return jsonify(error="job not found"), 404

    return jsonify(
        job_id=job_id,
        status=row["status"],
        result=row["result"],
        error=row["error_message"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        metrics=dict(
            queue_wait_seconds=row["queue_wait_seconds"],
            inference_seconds=row["inference_seconds"],
            total_seconds=row["total_seconds"],
            cpu_seconds=row["cpu_seconds"],
            cpu_utilization_pct=row["cpu_utilization_pct"],
            peak_rss_mb=row["peak_rss_mb"],
            input_chars=row["input_chars"],
            output_chars=row["output_chars"],
            model_version=row["model_version"],
            max_tokens=row["max_tokens"],
        ),
    )


@app.get("/metrics")
def metrics():
    """Aggregate benchmark numbers across every completed job and every
    worker boot so far, grouped by exact model file. This is the data
    Phase 2A exists to collect — use it to decide what to optimize
    instead of guessing."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT model_version, COUNT(*) AS completed_jobs, "
                "AVG(queue_wait_seconds) AS avg_queue_wait_seconds, "
                "AVG(inference_seconds) AS avg_inference_seconds, "
                "AVG(total_seconds) AS avg_total_seconds, "
                "AVG(cpu_utilization_pct) AS avg_cpu_utilization_pct, "
                "AVG(peak_rss_mb) AS avg_peak_rss_mb, "
                "AVG(input_chars) AS avg_input_chars, "
                "AVG(output_chars) AS avg_output_chars, "
                "AVG(max_tokens) AS avg_max_tokens "
                "FROM jobs WHERE status = 'done' GROUP BY model_version"
            )
            job_stats = cur.fetchall()

            cur.execute(
                "SELECT model_version, COUNT(*) AS worker_boots, "
                "AVG(download_seconds) AS avg_download_seconds, "
                "AVG(load_seconds) AS avg_load_seconds, "
                "AVG(startup_seconds) AS avg_startup_seconds, "
                "SUM(cache_hit) / COUNT(*) * 100 AS cache_hit_rate_pct "
                "FROM worker_runs WHERE startup_seconds IS NOT NULL "
                "GROUP BY model_version"
            )
            worker_stats = cur.fetchall()
    finally:
        conn.close()

    def _round_floats(rows):
        for row in rows:
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 2)
        return rows

    return jsonify(
        job_benchmarks=_round_floats(job_stats),
        worker_startup_benchmarks=_round_floats(worker_stats),
    )


@app.get("/health")
def health():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM workers "
                "WHERE status IN ('ready', 'busy') "
                "AND last_heartbeat > NOW() - INTERVAL 2 MINUTE"
            )
            workers_ready = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'")
            pending = cur.fetchone()["n"]
    finally:
        conn.close()

    return jsonify(ok=True, workers_ready=workers_ready, pending_jobs=pending)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
