-- Phase 3A: model registry.
-- Run this once, any time after schema.sql.

-- The small, manually-curated catalog of base models Hoster actually
-- knows how to run. Registering a fine-tune means picking one of
-- these by name — this is "enter the base AI you're using" instead of
-- editing code, and it's also the enforcement point that stops someone
-- registering an architecture we have no conversion path for.
CREATE TABLE IF NOT EXISTS base_models (
    name            VARCHAR(100) PRIMARY KEY,   -- e.g. "Qwen/Qwen2.5-7B-Instruct"
    gguf_repo       VARCHAR(255) NOT NULL,       -- where the quantized base GGUF lives
    gguf_file       VARCHAR(255) NOT NULL,
    architecture    VARCHAR(50)  NOT NULL,       -- governs which conversion path applies
    supports_lora   BOOLEAN NOT NULL DEFAULT TRUE,
    notes           VARCHAR(500) NULL
);

INSERT IGNORE INTO base_models (name, gguf_repo, gguf_file, architecture, supports_lora, notes) VALUES
('Qwen/Qwen2.5-7B-Instruct', 'bartowski/Qwen2.5-7B-Instruct-GGUF', 'Qwen2.5-7B-Instruct-Q4_K_M.gguf', 'qwen2', TRUE,
 'Verified: LoRA-to-GGUF conversion tested end-to-end against a real adapter (28 layers, rank 16, q/k/v/o target modules).');

-- Qwen2.5-VL-3B-Instruct is NOT in this catalog yet, on purpose — it's
-- a vision-language model needing a different runtime (likely
-- transformers+peft directly, not llama.cpp), and that path hasn't
-- been attempted or proven. Add it here only once it has been.

-- One row per registered fine-tune.
CREATE TABLE IF NOT EXISTS models (
    id                      VARCHAR(36)  PRIMARY KEY,
    name                    VARCHAR(100) NOT NULL UNIQUE,  -- caller-chosen; THIS is the model_name used everywhere else (jobs, workers, orchestrator) — never the raw base model string
    base_model              VARCHAR(100) NOT NULL,          -- must match base_models.name
    adapter_repo_url        VARCHAR(500) NOT NULL,           -- GitHub repo holding the LoRA adapter files
    adapter_subpath         VARCHAR(255) NOT NULL DEFAULT '', -- path within that repo to adapter_config.json/adapter_model.safetensors, e.g. "model"
    input_type              ENUM('text','image','audio','file') NOT NULL DEFAULT 'text',
    accepted_file_types     VARCHAR(255) NULL,               -- only meaningful when input_type != 'text', e.g. "png,jpg,pdf"
    output_type             ENUM('text','image','video','audio','integration') NOT NULL DEFAULT 'text',
    status                  ENUM('registered','preparing','ready','failed') NOT NULL DEFAULT 'registered',
    status_detail           VARCHAR(500) NULL,               -- why it's in that status, especially for 'failed'
    lora_gguf_url           VARCHAR(500) NULL,                -- where workers fetch the converted adapter from, once ready
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
