"""
Hoster public API (deploy this on Render).

Endpoints:
    POST /run-ai     { "model_name": "...", "input": "..." }  -> { "job_id": "..." }
    GET  /result/<job_id>                                     -> { "status": ..., "result": ... }
    GET  /health                                               -> { "ok": true, "workers_ready": N }

This service never talks to GitHub or runs any model itself. It only
writes/reads rows in TiDB. GitHub Actions workers pick the jobs up on
their own schedule.
"""

import os
import sys
import uuid

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection  # noqa: E402

app = Flask(__name__)

MAX_INPUT_CHARS = 8000  # keep a single job from eating a worker's whole lifetime


@app.post("/run-ai")
def run_ai():
    body = request.get_json(silent=True) or {}
    model_name = body.get("model_name")
    input_text = body.get("input")

    if not model_name or not input_text:
        return jsonify(error="model_name and input are required"), 400
    if len(input_text) > MAX_INPUT_CHARS:
        return jsonify(error=f"input too long (max {MAX_INPUT_CHARS} chars)"), 400

    job_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, status, model_name, input) "
                "VALUES (%s, 'pending', %s, %s)",
                (job_id, model_name, input_text),
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
                "SELECT status, result, error_message, created_at, updated_at "
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
