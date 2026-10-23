-- 003_a4_provider_settings.sql — Phase A chunk 3 (A4: provider settings).
-- New table provider_settings: one row per (tenant, provider) holding ALL
-- configured field values for that provider (identifiers + secrets) in the
-- `fields` JSON document — the tenant secret store. Secrets in this table
-- are server-side only: the API returns masked forms exclusively.
-- The application also creates this table in this shape on boot
-- (db.init_db from the models); this file is the DDL of record for
-- upgrading a database created by 001/002.

CREATE TABLE provider_settings (
    setting_id VARCHAR(40) NOT NULL,
    tenant_id VARCHAR(40) NOT NULL REFERENCES tenants (tenant_id),
    provider VARCHAR(30) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    fields JSON NOT NULL DEFAULT '{}',
    last_test_status VARCHAR(20) NOT NULL DEFAULT '',
    last_test_message VARCHAR(500) NOT NULL DEFAULT '',
    last_test_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (setting_id),
    CONSTRAINT uq_tenant_provider UNIQUE (tenant_id, provider)
);
CREATE INDEX ix_provider_settings_tenant_id ON provider_settings (tenant_id);
