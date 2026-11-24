CREATE TABLE data_import_jobs (
    id uuid PRIMARY KEY,
    instrument_id text REFERENCES instruments NOT NULL,
    interval text NOT NULL,
    filename text NOT NULL,
    source text NOT NULL,
    idempotency_key text UNIQUE,
    status text NOT NULL DEFAULT 'QUEUED',
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    row_count bigint,
    error text
);
CREATE INDEX data_import_jobs_pending ON data_import_jobs (created_at) WHERE status='QUEUED';

