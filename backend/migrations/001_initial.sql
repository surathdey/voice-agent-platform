-- NOTE (post-A12 upgrade-path fix): the application applies this step
-- automatically at boot via the migration runner (backend/app/
-- migrations.py) — idempotent, per-object, dialect-aware (PostgreSQL
-- + SQLite), tracked in the schema_migrations table. This file
-- remains the DDL of record; the runner step of the same number is
-- its executable twin.
-- 001_initial.sql — Voice Agent Platform, Phase A (A1) initial schema.
-- Generated from the SQLAlchemy models in backend/app/models.py (PostgreSQL dialect).
-- The application also creates tables idempotently on boot (db.init_db);
-- this file is the DDL of record for managed deployments.

CREATE TABLE tenants (
	tenant_id VARCHAR(40) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	market VARCHAR(4) NOT NULL, 
	provider_mode_default VARCHAR(20) NOT NULL, 
	provider_credential_refs JSON NOT NULL, 
	from_numbers JSON NOT NULL, 
	concurrency_limit INTEGER NOT NULL, 
	recording_retention_days INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (tenant_id)
);

CREATE TABLE agent_configs (
	agent_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	stage VARCHAR(120) NOT NULL, 
	purpose VARCHAR(300) NOT NULL, 
	is_reference BOOLEAN NOT NULL, 
	prompt TEXT NOT NULL, 
	llm_model VARCHAR(120) NOT NULL, 
	stt_provider VARCHAR(60) NOT NULL, 
	stt_language VARCHAR(20) NOT NULL, 
	tts_provider VARCHAR(60) NOT NULL, 
	tts_voice_id VARCHAR(120) NOT NULL, 
	language_rule JSON NOT NULL, 
	captured_fields JSON NOT NULL, 
	max_duration_s INTEGER NOT NULL, 
	silence_prompt_after_s INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (agent_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id)
);

CREATE TABLE users (
	user_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	email VARCHAR(200) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	role VARCHAR(40) NOT NULL, 
	password_hash VARCHAR(300) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (user_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), 
	UNIQUE (email)
);

CREATE TABLE campaigns (
	campaign_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	agent_id VARCHAR(40) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	product VARCHAR(120) NOT NULL, 
	purpose TEXT NOT NULL, 
	provider_mode VARCHAR(20) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	schedule JSON NOT NULL, 
	retry_rules JSON NOT NULL, 
	routing_rules JSON NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (campaign_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), 
	FOREIGN KEY(agent_id) REFERENCES agent_configs (agent_id)
);

CREATE TABLE prompt_versions (
	version_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	agent_id VARCHAR(40) NOT NULL, 
	version INTEGER NOT NULL, 
	prompt TEXT NOT NULL, 
	note VARCHAR(300) NOT NULL, 
	created_by VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (version_id), 
	CONSTRAINT uq_prompt_version UNIQUE (agent_id, version), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), 
	FOREIGN KEY(agent_id) REFERENCES agent_configs (agent_id)
);

CREATE TABLE leads (
	lead_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	campaign_id VARCHAR(40) NOT NULL, 
	phone_number VARCHAR(30) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	bank_name VARCHAR(200) NOT NULL, 
	state VARCHAR(120) NOT NULL, 
	agent_fields JSON NOT NULL, 
	attempt_count INTEGER NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (lead_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (campaign_id)
);

CREATE TABLE call_sessions (
	session_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	campaign_id VARCHAR(40) NOT NULL, 
	lead_id VARCHAR(40) NOT NULL, 
	attempt_id VARCHAR(60) NOT NULL, 
	provider_mode VARCHAR(20) NOT NULL, 
	provider_call_id VARCHAR(120) NOT NULL, 
	state VARCHAR(30) NOT NULL, 
	from_number VARCHAR(30) NOT NULL, 
	to_number VARCHAR(30) NOT NULL, 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	answered_at TIMESTAMP WITHOUT TIME ZONE, 
	ended_at TIMESTAMP WITHOUT TIME ZONE, 
	recording_url VARCHAR(500) NOT NULL, 
	end_reason VARCHAR(60) NOT NULL, 
	transcript JSON NOT NULL, 
	PRIMARY KEY (session_id), 
	CONSTRAINT uq_session_attempt UNIQUE (tenant_id, attempt_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), 
	FOREIGN KEY(campaign_id) REFERENCES campaigns (campaign_id), 
	FOREIGN KEY(lead_id) REFERENCES leads (lead_id)
);

CREATE TABLE call_results (
	result_id VARCHAR(40) NOT NULL, 
	tenant_id VARCHAR(40) NOT NULL, 
	session_id VARCHAR(40) NOT NULL, 
	disposition VARCHAR(60) NOT NULL, 
	confidence FLOAT NOT NULL, 
	summary TEXT NOT NULL, 
	captured_fields JSON NOT NULL, 
	duration_s INTEGER NOT NULL, 
	cost_breakdown JSON NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (result_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), 
	UNIQUE (session_id), 
	FOREIGN KEY(session_id) REFERENCES call_sessions (session_id)
);
