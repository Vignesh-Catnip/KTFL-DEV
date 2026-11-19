-- Approved migration (Decision 2): ONE new table. No existing table is altered.
-- The application also creates it automatically at start-up (database.ensure_schema -> create_all).
-- Rollback: DROP TABLE extraction_runs;   (nothing else references it)
CREATE TABLE IF NOT EXISTS extraction_runs (
    id                       SERIAL PRIMARY KEY,
    job_id                   INTEGER,
    invoice_id               INTEGER,
    filename                 VARCHAR(255),
    call_kind                VARCHAR(30) NOT NULL DEFAULT 'INVOICE',
    page_from                INTEGER,
    page_to                  INTEGER,
    pages_total              INTEGER,
    model                    VARCHAR(80),
    prompt_version           VARCHAR(40),
    prompt_hash              VARCHAR(64),
    raw_response             TEXT,
    parsed_json              JSON,
    done_reason              VARCHAR(30),
    prompt_eval_count        INTEGER,
    eval_count               INTEGER,
    total_duration_ms        INTEGER,
    load_duration_ms         INTEGER,
    prompt_eval_duration_ms  INTEGER,
    eval_duration_ms         INTEGER,
    status                   VARCHAR(20) NOT NULL DEFAULT 'OK',
    error                    TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_extraction_runs_job ON extraction_runs (job_id);
CREATE INDEX IF NOT EXISTS ix_extraction_runs_invoice ON extraction_runs (invoice_id);
