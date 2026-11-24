CREATE TABLE instruments (
    instrument_id text PRIMARY KEY,
    market_type text NOT NULL,
    dex text NOT NULL,
    asset_key text NOT NULL,
    exchange_asset_id bigint NOT NULL,
    symbol text NOT NULL,
    base text NOT NULL,
    quote text,
    rules jsonb NOT NULL,
    status text NOT NULL,
    listed_at timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    paper_supported boolean NOT NULL,
    UNIQUE (market_type, dex, asset_key)
);
CREATE TABLE market_snapshots (
    instrument_id text REFERENCES instruments NOT NULL,
    as_of timestamptz NOT NULL,
    mark numeric, mid numeric, oracle numeric, volume_24h numeric, change_24h numeric,
    open_interest numeric, funding numeric, market_cap numeric, bid numeric, ask numeric,
    source text NOT NULL, quality text NOT NULL,
    PRIMARY KEY (instrument_id, as_of)
);
CREATE TABLE candles (
    instrument_id text REFERENCES instruments NOT NULL,
    interval text NOT NULL,
    open_ts timestamptz NOT NULL, close_ts timestamptz NOT NULL,
    o numeric NOT NULL, h numeric NOT NULL, l numeric NOT NULL, c numeric NOT NULL, v numeric NOT NULL,
    trades integer NOT NULL, is_closed boolean NOT NULL, source text NOT NULL,
    received_ts timestamptz NOT NULL,
    PRIMARY KEY (instrument_id, interval, open_ts)
);
CREATE TABLE data_gaps (
    instrument_id text REFERENCES instruments NOT NULL,
    data_type text NOT NULL,
    start_ts timestamptz NOT NULL, end_ts timestamptz NOT NULL,
    reason text NOT NULL, recoverable boolean NOT NULL,
    PRIMARY KEY (instrument_id, data_type, start_ts)
);
CREATE TABLE market_events (
    event_id text PRIMARY KEY, instrument_id text REFERENCES instruments NOT NULL,
    event_type text NOT NULL, exchange_ts timestamptz NOT NULL, received_ts timestamptz NOT NULL,
    source text NOT NULL, schema_version integer NOT NULL, payload jsonb NOT NULL
);
CREATE INDEX market_events_latest ON market_events (instrument_id, event_type, exchange_ts DESC);
CREATE INDEX market_events_retention ON market_events (received_ts);
CREATE TABLE watchlists (
    id uuid PRIMARY KEY, name text NOT NULL, type text NOT NULL, config jsonb NOT NULL,
    version integer NOT NULL DEFAULT 1, enabled boolean NOT NULL DEFAULT true,
    last_refresh timestamptz, deleted_at timestamptz
);
CREATE TABLE watchlist_members (
    watchlist_id uuid REFERENCES watchlists NOT NULL,
    instrument_id text REFERENCES instruments NOT NULL,
    manual boolean NOT NULL DEFAULT false, pinned boolean NOT NULL DEFAULT false,
    blocked boolean NOT NULL DEFAULT false, automatic boolean NOT NULL DEFAULT false,
    entered_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (watchlist_id, instrument_id)
);
CREATE TABLE selection_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    watchlist_id uuid REFERENCES watchlists NOT NULL, as_of timestamptz NOT NULL,
    version integer NOT NULL, config jsonb NOT NULL, warnings jsonb NOT NULL
);
CREATE TABLE selection_results (
    run_id bigint REFERENCES selection_runs NOT NULL, instrument_id text REFERENCES instruments NOT NULL,
    rank integer, score numeric, reason text NOT NULL, action text NOT NULL,
    data_quality text NOT NULL, data_as_of timestamptz, sources jsonb NOT NULL,
    PRIMARY KEY (run_id, instrument_id)
);
CREATE TABLE worker_leases (
    component text PRIMARY KEY, heartbeat timestamptz NOT NULL, state jsonb NOT NULL
);
CREATE TABLE history_jobs (
    instrument_id text REFERENCES instruments NOT NULL, interval text NOT NULL,
    next_due timestamptz NOT NULL DEFAULT now(), progress_ts timestamptz,
    PRIMARY KEY (instrument_id, interval)
);

