-- Phase 2A: performance intelligence.
-- Run this once against the same TiDB database as schema.sql, after it.

-- Per-job metrics: everything we can measure about one request.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at         TIMESTAMP NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completed_at       TIMESTAMP NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS queue_wait_seconds FLOAT NULL;   -- created_at -> claimed_at
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS inference_seconds  FLOAT NULL;   -- wall-clock time inside the model call
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS total_seconds      FLOAT NULL;   -- created_at -> completed_at (what the caller actually felt)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cpu_seconds        FLOAT NULL;   -- process CPU time consumed during inference
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cpu_utilization_pct FLOAT NULL; -- cpu_seconds / (inference_seconds * core_count) * 100
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS peak_rss_mb        FLOAT NULL;   -- process peak resident memory at completion (whole-process, not job-isolated — see README caveat)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_chars        INT NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS output_chars       INT NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS model_version      VARCHAR(150) NULL;  -- the exact GGUF file used
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_tokens         INT NOT NULL DEFAULT 300;  -- caller-tunable generation cap, see README speed section

-- Per-worker-boot metrics: one row per GitHub Actions run of the worker.
CREATE TABLE IF NOT EXISTS worker_runs (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    worker_id         VARCHAR(100) NOT NULL,
    model_name        VARCHAR(100) NOT NULL,
    model_version     VARCHAR(150) NOT NULL,
    download_seconds  FLOAT NULL,
    load_seconds      FLOAT NULL,
    startup_seconds   FLOAT NULL,   -- download_seconds + load_seconds = time to first "ready"
    cache_hit         BOOLEAN NULL, -- was the model already in the GitHub Actions cache?
    started_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at          TIMESTAMP NULL,
    jobs_completed    INT NOT NULL DEFAULT 0,
    jobs_failed       INT NOT NULL DEFAULT 0,
    INDEX idx_model_started (model_version, started_at)
);
