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
    """Aggregate benchmark numbers, grouped by exact model file. This is
    the data Phase 2A exists to collect — use it to decide what to
    optimize instead of guessing.

    By default this covers every completed job ever, which means it
    mixes data from before and after any code change you make — a
    genuine improvement can be invisible in the lifetime average for a
    while, diluted by older/slower jobs. Pass ?since_minutes=30 to see
    only jobs/worker boots from the last 30 minutes, e.g. right after
    deploying a change you're trying to verify.
    """
    since_minutes = request.args.get("since_minutes", type=int)
    job_time_filter = "AND completed_at >= NOW() - INTERVAL %s MINUTE" if since_minutes else ""
    worker_time_filter = "AND started_at >= NOW() - INTERVAL %s MINUTE" if since_minutes else ""
    params = (since_minutes,) if since_minutes else ()

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
                f"FROM jobs WHERE status = 'done' {job_time_filter} GROUP BY model_version",
                params,
            )
            job_stats = cur.fetchall()

            cur.execute(
                "SELECT model_version, COUNT(*) AS worker_boots, "
                "AVG(download_seconds) AS avg_download_seconds, "
                "AVG(load_seconds) AS avg_load_seconds, "
                "AVG(startup_seconds) AS avg_startup_seconds, "
                "SUM(cache_hit) / COUNT(*) * 100 AS cache_hit_rate_pct "
                f"FROM worker_runs WHERE startup_seconds IS NOT NULL {worker_time_filter} "
                "GROUP BY model_version",
                params,
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


@app.get("/jobs")
def recent_jobs():
    """Phase 2B observability: see which worker claimed which job, live.
    GET /jobs?limit=20 (default 20, max 100)."""
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
    except ValueError:
        limit = 20

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, model_name, status, claimed_by, attempt_count, "
                "created_at, claimed_at, completed_at "
                "FROM jobs ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        for k in ("created_at", "claimed_at", "completed_at"):
            if row[k] is not None:
                row[k] = str(row[k])

    return jsonify(jobs=rows)


@app.get("/workers")
def list_workers():
    """Phase 2B observability: see every worker that's registered
    itself, its current status, and its lifetime counters — watch
    several run independently, and see one go offline without the
    others being affected."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, available_models, status, current_job_id, "
                "jobs_completed, jobs_failed, started_at, last_heartbeat "
                "FROM workers ORDER BY last_heartbeat DESC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        for k in ("started_at", "last_heartbeat"):
            if row[k] is not None:
                row[k] = str(row[k])

    return jsonify(workers=rows)


@app.get("/orchestrator/log")
def orchestrator_log():
    """Phase 2C observability: every scale up/down/no-change decision
    the orchestrator has made, with the real numbers behind it (pending
    jobs, committed workers, desired workers) — so 'why did it do that'
    always has a real answer. GET /orchestrator/log?limit=20&model=..."""
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
    except ValueError:
        limit = 20
    model_name = request.args.get("model")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if model_name:
                cur.execute(
                    "SELECT * FROM orchestrator_log WHERE model_name = %s "
                    "ORDER BY run_at DESC LIMIT %s",
                    (model_name, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM orchestrator_log ORDER BY run_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        if row.get("run_at") is not None:
            row["run_at"] = str(row["run_at"])

    return jsonify(decisions=rows)


@app.post("/control/pause")
def pause_system():
    """The actual kill switch. Flips a single DB flag that every worker
    and the orchestrator check for themselves — no GitHub token needed,
    no waiting on anyone. A worker already mid-job finishes that one
    job (so nothing already in flight is abruptly lost) but will not
    claim another, and will not reschedule itself. The orchestrator
    will not launch or stop anything while paused."""
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "")[:255]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE system_control SET paused = TRUE, reason = %s, "
                "paused_at = NOW() WHERE id = 1",
                (reason,),
            )
    finally:
        conn.close()

    return jsonify(paused=True, reason=reason)


@app.post("/control/resume")
def resume_system():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE system_control SET paused = FALSE, resumed_at = NOW() WHERE id = 1"
            )
    finally:
        conn.close()

    return jsonify(paused=False)


@app.get("/control/status")
def control_status():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM system_control WHERE id = 1")
            row = cur.fetchone()
    finally:
        conn.close()

    if row:
        for k in ("paused_at", "resumed_at"):
            if row.get(k) is not None:
                row[k] = str(row[k])

    return jsonify(row or {"paused": False})


@app.post("/models/register")
def register_model():
    """Register a fine-tuned model. `name` is yours to choose, and
    becomes the model_name used everywhere from here on (job
    submissions, worker assignment, orchestration) — never the raw
    base model string, specifically so two fine-tunes of the same base
    never collide."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    base_model = (body.get("base_model") or "").strip()
    adapter_repo_url = (body.get("adapter_repo_url") or "").strip()
    adapter_subpath = (body.get("adapter_subpath") or "").strip()
    input_type = body.get("input_type", "text")
    accepted_file_types = body.get("accepted_file_types")
    output_type = body.get("output_type", "text")

    if not name or not base_model or not adapter_repo_url:
        return jsonify(error="name, base_model, and adapter_repo_url are required"), 400
    if input_type not in ("text", "image", "audio", "file"):
        return jsonify(error="input_type must be one of: text, image, audio, file"), 400
    if output_type not in ("text", "image", "video", "audio", "integration"):
        return jsonify(error="output_type must be one of: text, image, video, audio, integration"), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM base_models WHERE name = %s", (base_model,))
            if cur.fetchone() is None:
                return jsonify(
                    error=f"'{base_model}' is not a supported base model. "
                          f"GET /base-models for the current list."
                ), 400

            cur.execute("SELECT 1 FROM models WHERE name = %s", (name,))
            if cur.fetchone() is not None:
                return jsonify(error=f"a model named '{name}' is already registered"), 409

            model_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO models (id, name, base_model, adapter_repo_url, adapter_subpath, "
                "input_type, accepted_file_types, output_type) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (model_id, name, base_model, adapter_repo_url, adapter_subpath,
                 input_type, accepted_file_types, output_type),
            )
    finally:
        conn.close()

    return jsonify(id=model_id, name=name, status="registered"), 201


@app.get("/base-models")
def list_base_models():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM base_models")
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify(base_models=rows)


def _model_with_stats(conn, row):
    """Merge a models row with live usage stats computed straight from
    the jobs table — never a stored counter that can drift from
    reality, same principle as everything else in this system."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS total_calls, "
            "SUM(status = 'done') AS successful, "
            "SUM(status = 'failed') AS failed, "
            "SUM(status IN ('pending', 'processing')) AS in_flight, "
            "AVG(inference_seconds) AS avg_inference_seconds, "
            "MAX(created_at) AS last_used_at "
            "FROM jobs WHERE model_name = %s",
            (row["name"],),
        )
        stats = cur.fetchone()

    total = stats["total_calls"] or 0
    successful = stats["successful"] or 0
    row["usage"] = dict(
        total_calls=total,
        successful=successful,
        failed=stats["failed"] or 0,
        in_flight=stats["in_flight"] or 0,
        success_rate_pct=round(successful / total * 100, 1) if total else None,
        avg_inference_seconds=round(stats["avg_inference_seconds"], 2) if stats["avg_inference_seconds"] else None,
        last_used_at=str(stats["last_used_at"]) if stats["last_used_at"] else None,
    )
    for k in ("created_at", "updated_at"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


@app.get("/models")
def list_models():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM models ORDER BY created_at DESC")
            rows = cur.fetchall()
        rows = [_model_with_stats(conn, r) for r in rows]
    finally:
        conn.close()
    return jsonify(models=rows)


@app.get("/models/<id_or_name>")
def get_model(id_or_name):
    """Look up by id or by the chosen name, either works."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM models WHERE id = %s OR name = %s", (id_or_name, id_or_name)
            )
            row = cur.fetchone()
        if row is None:
            return jsonify(error="model not found"), 404
        row = _model_with_stats(conn, row)
    finally:
        conn.close()
    return jsonify(row)


@app.patch("/models/<id_or_name>")
def update_model(id_or_name):
    """Updating base_model, adapter_repo_url, or adapter_subpath
    invalidates whatever was previously prepared — this isn't just a
    metadata edit, the old converted artifact (if any) no longer
    matches, so status resets to 'registered' and must go through
    preparation again before it's usable."""
    body = request.get_json(silent=True) or {}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM models WHERE id = %s OR name = %s", (id_or_name, id_or_name)
            )
            row = cur.fetchone()
            if row is None:
                return jsonify(error="model not found"), 404

            fields, values = [], []
            invalidating = False
            for key in ("base_model", "adapter_repo_url", "adapter_subpath"):
                if key in body:
                    fields.append(f"{key} = %s")
                    values.append(body[key])
                    invalidating = True
            for key in ("input_type", "accepted_file_types", "output_type"):
                if key in body:
                    fields.append(f"{key} = %s")
                    values.append(body[key])

            if not fields:
                return jsonify(error="no recognized fields to update"), 400

            if invalidating:
                fields += ["status = 'registered'", "status_detail = NULL", "lora_gguf_url = NULL"]

            values.append(row["id"])
            cur.execute(f"UPDATE models SET {', '.join(fields)} WHERE id = %s", values)
    finally:
        conn.close()

    return jsonify(id=row["id"], name=row["name"], updated=True, reset_to_registered=invalidating)


@app.delete("/models/<id_or_name>")
def delete_model(id_or_name):
    """Removes the registration entirely — the adapter link, base
    model assignment, and converted artifact reference are gone. Past
    job history under this model's name is left alone (it's a record
    of what happened, not a pointer to something that must still
    exist); only NEW submissions under this name will start failing,
    since there's no longer a registered model to resolve it to.
    Requires {"confirm": true} since this can't be undone from here."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify(error='pass {"confirm": true} in the request body to actually delete'), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM models WHERE id = %s OR name = %s", (id_or_name, id_or_name))
            deleted = cur.rowcount
    finally:
        conn.close()

    if not deleted:
        return jsonify(error="model not found"), 404
    return jsonify(deleted=True)


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
