ALTER TABLE market_snapshots ADD COLUMN cap_as_of timestamptz;
ALTER TABLE market_snapshots ADD COLUMN cap_source text;

