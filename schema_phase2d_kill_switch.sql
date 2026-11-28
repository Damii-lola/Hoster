-- Kill switch: a single row, a single flag. Every worker and the
-- orchestrator check this before doing anything. You control it
-- yourself via the API — no GitHub token, no waiting on Claude.

CREATE TABLE IF NOT EXISTS system_control (
    id          TINYINT PRIMARY KEY DEFAULT 1,
    paused      BOOLEAN NOT NULL DEFAULT FALSE,
    reason      VARCHAR(255) NULL,
    paused_at   TIMESTAMP NULL,
    resumed_at  TIMESTAMP NULL
);

INSERT IGNORE INTO system_control (id, paused) VALUES (1, FALSE);
