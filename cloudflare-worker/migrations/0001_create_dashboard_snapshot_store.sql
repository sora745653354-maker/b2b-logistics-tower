CREATE TABLE IF NOT EXISTS tower_batches (
  batch_id INTEGER PRIMARY KEY,
  file_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('staging', 'published')),
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  published_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_tower_batches_published
  ON tower_batches(status, batch_id DESC);

CREATE TABLE IF NOT EXISTS tower_snapshot_parts (
  batch_id INTEGER NOT NULL REFERENCES tower_batches(batch_id) ON DELETE CASCADE,
  part_type TEXT NOT NULL CHECK (part_type IN ('records', 'transitions', 'outbound')),
  part_index INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (batch_id, part_type, part_index)
);

CREATE INDEX IF NOT EXISTS ix_tower_snapshot_parts_batch
  ON tower_snapshot_parts(batch_id, part_type, part_index);
