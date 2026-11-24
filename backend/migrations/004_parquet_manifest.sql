CREATE TABLE market_data_manifest (
    dataset_id text NOT NULL,
    partition_date date NOT NULL,
    market_type text NOT NULL,
    path text NOT NULL UNIQUE,
    checksum text NOT NULL,
    schema_version integer NOT NULL,
    start_ts timestamptz NOT NULL,
    end_ts timestamptz NOT NULL,
    row_count bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, partition_date, market_type, checksum)
);

