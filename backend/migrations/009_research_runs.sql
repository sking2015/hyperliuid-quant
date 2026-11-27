CREATE TABLE research_configs (
    hash text PRIMARY KEY, detector_version text NOT NULL, feature_version text NOT NULL,
    parameters jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE research_runs (
    id uuid PRIMARY KEY, config_hash text NOT NULL REFERENCES research_configs(hash),
    mode text NOT NULL CHECK(mode IN ('LIVE','REPLAY')), asset_ids text[] NOT NULL,
    start_ms bigint NOT NULL, end_ms bigint NOT NULL, cursor_ms bigint,
    status text NOT NULL DEFAULT 'QUEUED', stop_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    stats jsonb NOT NULL DEFAULT '{}', error text, source_hash text,
    CHECK(end_ms > start_ms)
);
CREATE INDEX research_runs_pending ON research_runs (status,created_at);
CREATE TABLE research_events (
    id text PRIMARY KEY, run_id uuid NOT NULL REFERENCES research_runs(id),
    instrument_id text NOT NULL REFERENCES instruments(instrument_id), variant text NOT NULL,
    detected_ms bigint NOT NULL, direction text NOT NULL, episode_id text NOT NULL,
    state text NOT NULL, detail jsonb NOT NULL
);
CREATE INDEX research_events_by_run ON research_events(run_id,detected_ms DESC,id);
CREATE TABLE research_event_transitions (
    event_id text NOT NULL REFERENCES research_events(id), sequence integer NOT NULL,
    state text NOT NULL, at_ms bigint NOT NULL, reasons jsonb NOT NULL,
    PRIMARY KEY(event_id,sequence)
);
CREATE TABLE research_labels (
    event_id text NOT NULL REFERENCES research_events(id), horizon_s integer NOT NULL,
    version text NOT NULL, status text NOT NULL, detail jsonb NOT NULL,
    PRIMARY KEY(event_id,horizon_s,version)
);
CREATE TABLE research_data_pins (
    run_id uuid NOT NULL REFERENCES research_runs(id),
    path text NOT NULL REFERENCES market_data_manifest(path),
    PRIMARY KEY(run_id,path)
);

