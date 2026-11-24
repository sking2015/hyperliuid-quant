CREATE TABLE candle_imports (
    instrument_id text REFERENCES instruments NOT NULL,
    interval text NOT NULL,
    checksum text NOT NULL,
    source text NOT NULL,
    rows bigint NOT NULL,
    start_ts timestamptz NOT NULL,
    end_ts timestamptz NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id,interval,checksum,source)
);

