CREATE TABLE funding_rates (
    instrument_id text REFERENCES instruments NOT NULL,
    exchange_ts timestamptz NOT NULL,
    funding_rate numeric NOT NULL,
    premium numeric,
    received_ts timestamptz NOT NULL,
    source text NOT NULL,
    PRIMARY KEY (instrument_id, exchange_ts)
);
CREATE TABLE funding_jobs (
    instrument_id text PRIMARY KEY REFERENCES instruments,
    next_due timestamptz NOT NULL DEFAULT now(),
    progress_ts timestamptz
);

