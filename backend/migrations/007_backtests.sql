CREATE TABLE backtest_runs (
    id uuid PRIMARY KEY,
    configuration jsonb NOT NULL,
    status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
    progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    report jsonb,
    error text
);
CREATE INDEX backtest_queue ON backtest_runs (status, created_at);
CREATE TABLE backtest_ledger (
    run_id uuid NOT NULL REFERENCES backtest_runs(id),
    event_key text NOT NULL,
    entry jsonb NOT NULL,
    PRIMARY KEY (run_id, event_key)
);

