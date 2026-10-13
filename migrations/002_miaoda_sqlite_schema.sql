-- Additive PostgreSQL mirror of the local SQLite ingestion schema.
-- Existing MiaoDa app tables are left untouched; these tables form the
-- normalized ingestion/history layer to which the app backend can be migrated.

CREATE TABLE IF NOT EXISTS etl_ingest_batch (
  batch_id BIGSERIAL PRIMARY KEY,
  source_system TEXT NOT NULL DEFAULT 'TMS',
  file_name TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  file_storage_uri TEXT NOT NULL,
  report_scope TEXT,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  row_count INTEGER NOT NULL DEFAULT 0,
  valid_soo_count INTEGER NOT NULL DEFAULT 0,
  reject_count INTEGER NOT NULL DEFAULT 0,
  quality JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL,
  error_message TEXT,
  UNIQUE (source_system, file_sha256)
);

CREATE TABLE IF NOT EXISTS ods_tms_soo_raw (
  batch_id BIGINT NOT NULL REFERENCES etl_ingest_batch(batch_id),
  row_no INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  soo_no TEXT NOT NULL,
  payload JSONB NOT NULL,
  quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (batch_id, row_no)
);

CREATE TABLE IF NOT EXISTS dwd_soo_snapshot (
  batch_id BIGINT NOT NULL REFERENCES etl_ingest_batch(batch_id),
  domain_code TEXT NOT NULL,
  soo_no TEXT NOT NULL,
  source_updated_at TIMESTAMPTZ,
  normalized_row JSONB NOT NULL,
  PRIMARY KEY (batch_id, domain_code, soo_no)
);

CREATE TABLE IF NOT EXISTS dwd_soo_current (
  domain_code TEXT NOT NULL,
  soo_no TEXT NOT NULL,
  production_date DATE,
  source_updated_at TIMESTAMPTZ,
  last_batch_id BIGINT NOT NULL REFERENCES etl_ingest_batch(batch_id),
  normalized_row JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (domain_code, soo_no)
);

CREATE TABLE IF NOT EXISTS collector_job (
  job_id BIGSERIAL PRIMARY KEY,
  job_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  batch_id BIGINT REFERENCES etl_ingest_batch(batch_id),
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS tms_sync_run (
  run_id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  report_start TEXT NOT NULL,
  report_end TEXT NOT NULL,
  batch_id BIGINT REFERENCES etl_ingest_batch(batch_id),
  valid_soo_count INTEGER,
  unchanged BOOLEAN NOT NULL DEFAULT FALSE,
  error_code TEXT,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS sync_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tms_export_artifact (
  file_path TEXT NOT NULL,
  file_size BIGINT NOT NULL,
  file_mtime_ns BIGINT NOT NULL,
  run_id BIGINT NOT NULL REFERENCES tms_sync_run(run_id),
  PRIMARY KEY (file_path, file_size, file_mtime_ns)
);

CREATE TABLE IF NOT EXISTS pickup_status_transition (
  transition_id BIGSERIAL PRIMARY KEY,
  from_batch_id BIGINT NOT NULL REFERENCES etl_ingest_batch(batch_id),
  to_batch_id BIGINT NOT NULL REFERENCES etl_ingest_batch(batch_id),
  interval_start TIMESTAMPTZ NOT NULL,
  interval_end TIMESTAMPTZ NOT NULL,
  domain_code TEXT NOT NULL,
  soo_no TEXT NOT NULL,
  waybill_id TEXT,
  warehouse_name TEXT,
  carrier_name TEXT,
  channel TEXT,
  production_date DATE,
  business_dates JSONB NOT NULL DEFAULT '[]'::jsonb,
  previous_status TEXT,
  current_status TEXT NOT NULL,
  soo_qty NUMERIC,
  gross_cbm NUMERIC,
  sp_volume_counted BOOLEAN NOT NULL DEFAULT FALSE,
  is_self_pickup BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (to_batch_id, domain_code, soo_no)
);

CREATE TABLE IF NOT EXISTS tms_hourly_source_snapshot (
  hour_slot TEXT PRIMARY KEY,
  batch_id BIGINT NOT NULL REFERENCES etl_ingest_batch(batch_id),
  source_snapshot_at TIMESTAMPTZ NOT NULL,
  saved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dwd_soo_current_production_date
  ON dwd_soo_current (production_date);
CREATE INDEX IF NOT EXISTS ix_dwd_soo_current_last_batch
  ON dwd_soo_current (last_batch_id);
CREATE INDEX IF NOT EXISTS ix_dwd_soo_snapshot_batch
  ON dwd_soo_snapshot (batch_id);
CREATE INDEX IF NOT EXISTS ix_pickup_transition_interval
  ON pickup_status_transition (interval_end, warehouse_name, carrier_name);
CREATE INDEX IF NOT EXISTS ix_pickup_transition_business_dates
  ON pickup_status_transition USING GIN (business_dates);
CREATE INDEX IF NOT EXISTS ix_hourly_source_snapshot_batch
  ON tms_hourly_source_snapshot (batch_id);

