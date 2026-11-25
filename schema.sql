-- Hoster queue schema (TiDB / MySQL-compatible)
-- Run this once against your TiDB Cloud database before starting any worker or API.

CREATE TABLE IF NOT EXISTS jobs (
    id              VARCHAR(36)  PRIMARY KEY,
    status          ENUM('pending', 'processing', 'done', 'failed') NOT NULL DEFAULT 'pending',
    model_name      VARCHAR(100) NOT NULL,
    input           TEXT         NOT NULL,
    result          LONGTEXT     NULL,
    error_message   TEXT         NULL,
    claimed_by      VARCHAR(100) NULL,
    attempt_count   INT          NOT NULL DEFAULT 0,
    max_attempts    INT          NOT NULL DEFAULT 3,
    lease_expires_at TIMESTAMP   NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_status_created (status, created_at)
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id         VARCHAR(100) PRIMARY KEY,
    available_models  VARCHAR(255) NOT NULL,
    status            ENUM('starting', 'ready', 'busy', 'shutting_down', 'offline') NOT NULL DEFAULT 'starting',
    current_job_id    VARCHAR(36)  NULL,
    jobs_completed    INT          NOT NULL DEFAULT 0,
    jobs_failed       INT          NOT NULL DEFAULT 0,
    started_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
