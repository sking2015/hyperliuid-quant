CREATE SEQUENCE research_ingest_seq;
ALTER TABLE market_events ALTER COLUMN exchange_ts DROP NOT NULL;
ALTER TABLE market_events ADD COLUMN available_ts timestamptz;
ALTER TABLE market_events ADD COLUMN ingest_seq bigint;
ALTER TABLE market_events ADD COLUMN connection_id uuid;
ALTER TABLE market_events ADD COLUMN quality jsonb;
CREATE INDEX market_events_research_replay ON market_events (available_ts,ingest_seq,event_id)
WHERE ingest_seq IS NOT NULL;
CREATE INDEX market_events_research_asset ON market_events (instrument_id,event_type,available_ts)
WHERE ingest_seq IS NOT NULL;

