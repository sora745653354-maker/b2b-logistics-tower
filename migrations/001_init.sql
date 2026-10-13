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
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'quarantined')),
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
CREATE INDEX IF NOT EXISTS ix_soo_production_date ON dwd_soo_current(production_date);
CREATE INDEX IF NOT EXISTS ix_soo_current_batch ON dwd_soo_current(last_batch_id);
CREATE INDEX IF NOT EXISTS ix_soo_snapshot_batch ON dwd_soo_snapshot(batch_id);

CREATE TABLE IF NOT EXISTS app_data_version (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    published_batch_id BIGINT REFERENCES etl_ingest_batch(batch_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO app_data_version(singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS collector_job (
    job_id BIGSERIAL PRIMARY KEY,
    job_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'needs_login')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    batch_id BIGINT REFERENCES etl_ingest_batch(batch_id),
    error_message TEXT
);
