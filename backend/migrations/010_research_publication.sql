CREATE INDEX market_events_research_unpublished ON market_events(event_id)
    WHERE source='hyperliquid_research_ws_v3' AND ingest_seq IS NULL;

CREATE TABLE research_feed_publication (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    watermark timestamptz NOT NULL
);

CREATE TABLE research_stability_samples (
    minute timestamptz PRIMARY KEY,
    state jsonb NOT NULL
);

ALTER TABLE research_runs ADD COLUMN input_sequence bigint;
ALTER TABLE research_runs ADD COLUMN follow_end_ms bigint;
ALTER TABLE research_runs ADD COLUMN input_watermark_ms bigint;
ALTER TABLE research_runs ADD COLUMN preflight jsonb NOT NULL DEFAULT '{}';
ALTER TABLE research_runs ADD COLUMN code_hash text;
ALTER TABLE research_configs ADD COLUMN detector_id text NOT NULL DEFAULT 'S01';
