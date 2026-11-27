-- Phase 2C: worker orchestration.
-- Run this once against the same TiDB database, after the Phase 2A migration.

-- Lets the orchestrator find and cancel one specific worker's own
-- GitHub Actions run later, without touching any other run.
ALTER TABLE workers ADD COLUMN IF NOT EXISTS github_run_id BIGINT NULL;

-- One row per orchestration decision cycle per model, so every
-- "why did it scale up/down" question has a real, inspectable answer
-- instead of needing to be inferred after the fact.
CREATE TABLE IF NOT EXISTS orchestrator_log (
    id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model_name              VARCHAR(100) NOT NULL,
    pending_jobs            INT NOT NULL,
    committed_workers       INT NOT NULL,  -- auto-workers starting, ready, or busy (capacity already in flight)
    desired_workers         INT NOT NULL,
    action                  ENUM('scale_up', 'scale_down', 'no_change') NOT NULL,
    workers_launched        INT NOT NULL DEFAULT 0,
    workers_stopped         INT NOT NULL DEFAULT 0,
    detail                  VARCHAR(500) NULL,
    INDEX idx_model_time (model_name, run_at)
);
