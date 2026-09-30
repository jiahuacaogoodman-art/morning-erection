-- WakeCore Kernel schema v0.2 (WK-KERNEL-002), generated from wakecore/kernel/ports/tables.py.
-- Do not edit by hand: regenerate with
--   python -m wakecore.adapters.postgres.migration > packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql
--
-- Run as a migration role. The application role must be LOGIN NOSUPERUSER NOBYPASSRLS (RFC §9.4);
-- see docs/runbooks/postgres.md.

BEGIN;

CREATE TABLE IF NOT EXISTS "grants" (
  "tenant_id" TEXT NOT NULL,
  "grant_ref" TEXT NOT NULL,
  "principal" TEXT NOT NULL,
  "version" BIGINT NOT NULL,
  "capabilities" JSONB NOT NULL,
  "data_egress" JSONB NOT NULL,
  "resource_scope" JSONB NOT NULL,
  "expires_at" TIMESTAMPTZ,
  "revoked" BOOLEAN NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_grants" PRIMARY KEY ("tenant_id", "grant_ref")
);

CREATE TABLE IF NOT EXISTS "task_specs" (
  "tenant_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "spec_version" BIGINT NOT NULL,
  "root_task_id" TEXT NOT NULL,
  "spec_digest" TEXT NOT NULL,
  "body" JSONB NOT NULL,
  "status" TEXT NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  "created_by" TEXT NOT NULL,
  "confirmed_at" TIMESTAMPTZ,
  "confirmed_by" TEXT,
  CONSTRAINT "pk_task_specs" PRIMARY KEY ("tenant_id", "task_id", "spec_version")
);

CREATE TABLE IF NOT EXISTS "task_runtimes" (
  "tenant_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "root_task_id" TEXT NOT NULL,
  "parent_task_id" TEXT,
  "depth" BIGINT NOT NULL,
  "lifecycle" TEXT NOT NULL,
  "active_spec_version" BIGINT NOT NULL,
  "version" BIGINT NOT NULL,
  "revocation_epoch" BIGINT NOT NULL,
  "source_ref" TEXT NOT NULL,
  "grant_ref" TEXT NOT NULL,
  "grant_version" BIGINT NOT NULL,
  "effective_authority" JSONB NOT NULL,
  "expires_at" TIMESTAMPTZ NOT NULL,
  "lifecycle_reason" TEXT,
  "last_progress_at" TIMESTAMPTZ,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_task_runtimes" PRIMARY KEY ("tenant_id", "task_id"),
  FOREIGN KEY ("tenant_id", "task_id", "active_spec_version") REFERENCES "task_specs" ("tenant_id", "task_id", "spec_version")
);

CREATE INDEX IF NOT EXISTS "ix_task_runtimes_0" ON "task_runtimes" ("tenant_id", "root_task_id");

CREATE INDEX IF NOT EXISTS "ix_task_runtimes_1" ON "task_runtimes" ("tenant_id", "source_ref", "lifecycle");

CREATE TABLE IF NOT EXISTS "source_bindings" (
  "tenant_id" TEXT NOT NULL,
  "source_ref" TEXT NOT NULL,
  "owner" TEXT NOT NULL,
  "connector_id" TEXT NOT NULL,
  "source_uri" TEXT NOT NULL,
  "resource_scope" JSONB NOT NULL,
  "secret_ref" TEXT,
  "ingress_secret_ref" TEXT,
  "capabilities" JSONB NOT NULL,
  "health" TEXT NOT NULL,
  "health_reason" TEXT,
  "last_trusted_at" TIMESTAMPTZ,
  "last_attempt_at" TIMESTAMPTZ,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_source_bindings" PRIMARY KEY ("tenant_id", "source_ref"),
  CONSTRAINT "uq_source_bindings_source_ref" UNIQUE ("source_ref")
);

CREATE TABLE IF NOT EXISTS "source_checkpoints" (
  "tenant_id" TEXT NOT NULL,
  "source_ref" TEXT NOT NULL,
  "scope_key" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "local_revision" BIGINT NOT NULL,
  "trusted_revision" TEXT,
  "trusted_snapshot" JSONB,
  "trusted_observation_id" TEXT,
  "trusted_at" TIMESTAMPTZ,
  "cursor" TEXT,
  "version" BIGINT NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_source_checkpoints" PRIMARY KEY ("tenant_id", "source_ref", "scope_key"),
  FOREIGN KEY ("tenant_id", "source_ref") REFERENCES "source_bindings" ("tenant_id", "source_ref")
);

CREATE TABLE IF NOT EXISTS "triggers" (
  "tenant_id" TEXT NOT NULL,
  "trigger_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "root_task_id" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "every_seconds" BIGINT,
  "timezone" TEXT NOT NULL,
  "catchup_policy" TEXT NOT NULL,
  "due_at" TIMESTAMPTZ,
  "expiry" TIMESTAMPTZ,
  "generation" BIGINT NOT NULL,
  "status" TEXT NOT NULL,
  "predicate_ref" TEXT,
  "depth" BIGINT NOT NULL,
  "origin_run_id" TEXT,
  "followup_key" TEXT,
  "reason" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_triggers" PRIMARY KEY ("tenant_id", "trigger_id"),
  CONSTRAINT "uq_triggers_root_task_id_followup_key" UNIQUE ("tenant_id", "root_task_id", "followup_key")
);

CREATE INDEX IF NOT EXISTS "ix_triggers_0" ON "triggers" ("status", "due_at");

CREATE INDEX IF NOT EXISTS "ix_triggers_1" ON "triggers" ("tenant_id", "task_id");

CREATE TABLE IF NOT EXISTS "trigger_occurrences" (
  "tenant_id" TEXT NOT NULL,
  "trigger_id" TEXT NOT NULL,
  "generation" BIGINT NOT NULL,
  "scheduled_for" TIMESTAMPTZ NOT NULL,
  "task_id" TEXT NOT NULL,
  "occurred_at" TIMESTAMPTZ NOT NULL,
  "coalesced_count" BIGINT NOT NULL,
  "run_id" TEXT NOT NULL,
  CONSTRAINT "pk_trigger_occurrences" PRIMARY KEY ("tenant_id", "trigger_id", "generation", "scheduled_for"),
  FOREIGN KEY ("tenant_id", "trigger_id") REFERENCES "triggers" ("tenant_id", "trigger_id")
);

CREATE INDEX IF NOT EXISTS "ix_trigger_occurrences_0" ON "trigger_occurrences" ("tenant_id", "run_id");

CREATE TABLE IF NOT EXISTS "observations" (
  "tenant_id" TEXT NOT NULL,
  "observation_id" TEXT NOT NULL,
  "source_ref" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "scope" JSONB NOT NULL,
  "outcome" TEXT NOT NULL,
  "completeness" TEXT NOT NULL,
  "source_revision" TEXT,
  "observed_at" TIMESTAMPTZ NOT NULL,
  "payload_digest" TEXT,
  "evidence_ref" TEXT,
  "coverage_gaps" JSONB NOT NULL,
  "history_coverage" JSONB,
  "error_code" TEXT,
  "connector_version" TEXT NOT NULL,
  CONSTRAINT "pk_observations" PRIMARY KEY ("tenant_id", "observation_id")
);

CREATE INDEX IF NOT EXISTS "ix_observations_0" ON "observations" ("tenant_id", "task_id", "observed_at");

CREATE TABLE IF NOT EXISTS "evidence" (
  "tenant_id" TEXT NOT NULL,
  "evidence_ref" TEXT NOT NULL,
  "source_ref" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "digest" TEXT NOT NULL,
  "content" JSONB NOT NULL,
  "visibility" TEXT NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_evidence" PRIMARY KEY ("tenant_id", "evidence_ref")
);

CREATE TABLE IF NOT EXISTS "events" (
  "tenant_id" TEXT NOT NULL,
  "event_id" TEXT NOT NULL,
  "seq" BIGINT NOT NULL,
  "source_ref" TEXT NOT NULL,
  "source_uri" TEXT NOT NULL,
  "source_event_id" TEXT NOT NULL,
  "specversion" TEXT NOT NULL,
  "type" TEXT NOT NULL,
  "subject" TEXT,
  "occurred_at" TIMESTAMPTZ,
  "received_at" TIMESTAMPTZ NOT NULL,
  "payload_digest" TEXT NOT NULL,
  "data" JSONB NOT NULL,
  "verified" BOOLEAN NOT NULL,
  "internal" BOOLEAN NOT NULL,
  "ingress_principal" TEXT NOT NULL,
  "causation_id" TEXT,
  "evidence_ref" TEXT,
  CONSTRAINT "pk_events" PRIMARY KEY ("tenant_id", "event_id"),
  CONSTRAINT "uq_events_source_ref_source_event_id" UNIQUE ("tenant_id", "source_ref", "source_event_id")
);

CREATE INDEX IF NOT EXISTS "ix_events_0" ON "events" ("tenant_id", "seq");

CREATE TABLE IF NOT EXISTS "deliveries" (
  "tenant_id" TEXT NOT NULL,
  "event_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "run_id" TEXT,
  "decision_ref" TEXT,
  "reason" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_deliveries" PRIMARY KEY ("tenant_id", "event_id", "task_id"),
  FOREIGN KEY ("tenant_id", "event_id") REFERENCES "events" ("tenant_id", "event_id")
);

CREATE INDEX IF NOT EXISTS "ix_deliveries_0" ON "deliveries" ("tenant_id", "task_id", "status");

CREATE TABLE IF NOT EXISTS "decisions" (
  "tenant_id" TEXT NOT NULL,
  "decision_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "event_id" TEXT,
  "route" TEXT NOT NULL,
  "reason_code" TEXT NOT NULL,
  "evidence_refs" JSONB NOT NULL,
  "uncertainty_flags" JSONB NOT NULL,
  "suggested_capabilities" JSONB NOT NULL,
  "model_call_ref" TEXT,
  "policy_version" TEXT NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_decisions" PRIMARY KEY ("tenant_id", "decision_id")
);

CREATE INDEX IF NOT EXISTS "ix_decisions_0" ON "decisions" ("tenant_id", "task_id");

CREATE TABLE IF NOT EXISTS "runs" (
  "tenant_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "root_task_id" TEXT NOT NULL,
  "reason" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "wait_reason" TEXT,
  "checkpoint" JSONB NOT NULL,
  "spec_version" BIGINT NOT NULL,
  "revocation_epoch" BIGINT NOT NULL,
  "step_count" BIGINT NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  "finished_at" TIMESTAMPTZ,
  CONSTRAINT "pk_runs" PRIMARY KEY ("tenant_id", "run_id")
);

CREATE INDEX IF NOT EXISTS "ix_runs_0" ON "runs" ("tenant_id", "task_id", "status");

CREATE TABLE IF NOT EXISTS "steps" (
  "tenant_id" TEXT NOT NULL,
  "step_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "logical_step_id" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "input" JSONB NOT NULL,
  "result" JSONB,
  "available_at" TIMESTAMPTZ NOT NULL,
  "priority" BIGINT NOT NULL,
  "attempts" BIGINT NOT NULL,
  "max_attempts" BIGINT NOT NULL,
  "lease_owner" TEXT,
  "lease_epoch" BIGINT NOT NULL,
  "lease_until" TIMESTAMPTZ,
  "current_attempt_id" TEXT,
  "last_error" TEXT,
  "wait_reason" TEXT,
  "retry_owner" TEXT NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_steps" PRIMARY KEY ("tenant_id", "step_id"),
  CONSTRAINT "uq_steps_run_id_logical_step_id" UNIQUE ("tenant_id", "run_id", "logical_step_id"),
  FOREIGN KEY ("tenant_id", "run_id") REFERENCES "runs" ("tenant_id", "run_id")
);

CREATE INDEX IF NOT EXISTS "ix_steps_0" ON "steps" ("status", "available_at");

CREATE INDEX IF NOT EXISTS "ix_steps_1" ON "steps" ("status", "lease_until");

CREATE TABLE IF NOT EXISTS "wait_records" (
  "tenant_id" TEXT NOT NULL,
  "wait_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "step_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "reason" TEXT NOT NULL,
  "match" JSONB NOT NULL,
  "basis_watermark" BIGINT NOT NULL,
  "due_at" TIMESTAMPTZ,
  "status" TEXT NOT NULL,
  "matched_event_id" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  "resolved_at" TIMESTAMPTZ,
  CONSTRAINT "pk_wait_records" PRIMARY KEY ("tenant_id", "wait_id")
);

CREATE INDEX IF NOT EXISTS "ix_wait_records_0" ON "wait_records" ("status", "due_at");

CREATE TABLE IF NOT EXISTS "execution_slots" (
  "tenant_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "holder_step_id" TEXT NOT NULL,
  "lease_owner" TEXT NOT NULL,
  "lease_epoch" BIGINT NOT NULL,
  "lease_until" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_execution_slots" PRIMARY KEY ("tenant_id", "task_id")
);

CREATE TABLE IF NOT EXISTS "actions" (
  "tenant_id" TEXT NOT NULL,
  "action_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "root_task_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "effect_key" TEXT NOT NULL,
  "causal_event_id" TEXT,
  "logical_action" TEXT NOT NULL,
  "tool_id" TEXT NOT NULL,
  "tool_version" TEXT NOT NULL,
  "capability" TEXT NOT NULL,
  "canonical_payload" JSONB NOT NULL,
  "payload_digest" TEXT NOT NULL,
  "preconditions" JSONB NOT NULL,
  "resource_scope" JSONB NOT NULL,
  "data_egress" JSONB NOT NULL,
  "status" TEXT NOT NULL,
  "requires_approval" BOOLEAN NOT NULL,
  "revocation_epoch" BIGINT NOT NULL,
  "reason_code" TEXT,
  "resolution" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_actions" PRIMARY KEY ("tenant_id", "action_id"),
  CONSTRAINT "uq_actions_task_id_effect_key" UNIQUE ("tenant_id", "task_id", "effect_key")
);

CREATE INDEX IF NOT EXISTS "ix_actions_0" ON "actions" ("tenant_id", "root_task_id", "status");

CREATE INDEX IF NOT EXISTS "ix_actions_1" ON "actions" ("status");

CREATE TABLE IF NOT EXISTS "action_attempts" (
  "tenant_id" TEXT NOT NULL,
  "attempt_id" TEXT NOT NULL,
  "action_id" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "lease_owner" TEXT NOT NULL,
  "lease_epoch" BIGINT NOT NULL,
  "lease_until" TIMESTAMPTZ NOT NULL,
  "started_at" TIMESTAMPTZ NOT NULL,
  "finished_at" TIMESTAMPTZ,
  "provider_ref" TEXT,
  "provider_request_id" TEXT,
  "receipt" JSONB,
  "late_receipt" JSONB,
  "error" TEXT,
  CONSTRAINT "pk_action_attempts" PRIMARY KEY ("tenant_id", "attempt_id"),
  CONSTRAINT "uq_action_attempts_provider_ref_provider_request_id" UNIQUE ("tenant_id", "provider_ref", "provider_request_id"),
  FOREIGN KEY ("tenant_id", "action_id") REFERENCES "actions" ("tenant_id", "action_id")
);

CREATE INDEX IF NOT EXISTS "ix_action_attempts_0" ON "action_attempts" ("tenant_id", "action_id");

CREATE TABLE IF NOT EXISTS "approvals" (
  "tenant_id" TEXT NOT NULL,
  "approval_id" TEXT NOT NULL,
  "action_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "approval_revision" BIGINT NOT NULL,
  "payload_digest" TEXT NOT NULL,
  "grant_ref" TEXT NOT NULL,
  "grant_version" BIGINT NOT NULL,
  "expires_at" TIMESTAMPTZ NOT NULL,
  "decision" TEXT NOT NULL,
  "actor" TEXT,
  "decided_at" TIMESTAMPTZ,
  "reason" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_approvals" PRIMARY KEY ("tenant_id", "approval_id"),
  CONSTRAINT "uq_approvals_action_id_approval_revision" UNIQUE ("tenant_id", "action_id", "approval_revision"),
  FOREIGN KEY ("tenant_id", "action_id") REFERENCES "actions" ("tenant_id", "action_id")
);

CREATE TABLE IF NOT EXISTS "outbox" (
  "tenant_id" TEXT NOT NULL,
  "outbox_id" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "ref_id" TEXT NOT NULL,
  "dedupe_key" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "available_at" TIMESTAMPTZ NOT NULL,
  "attempts" BIGINT NOT NULL,
  "lease_owner" TEXT,
  "lease_epoch" BIGINT NOT NULL,
  "lease_until" TIMESTAMPTZ,
  "last_error" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_outbox" PRIMARY KEY ("tenant_id", "outbox_id"),
  CONSTRAINT "uq_outbox_dedupe_key" UNIQUE ("tenant_id", "dedupe_key")
);

CREATE INDEX IF NOT EXISTS "ix_outbox_0" ON "outbox" ("status", "available_at");

CREATE TABLE IF NOT EXISTS "budget_accounts" (
  "tenant_id" TEXT NOT NULL,
  "account_key" TEXT NOT NULL,
  "scope" TEXT NOT NULL,
  "subject_id" TEXT NOT NULL,
  "metric" TEXT NOT NULL,
  "period_key" TEXT NOT NULL,
  "unit" TEXT NOT NULL,
  "limit_amount" BIGINT NOT NULL,
  "reserved" BIGINT NOT NULL,
  "settled" BIGINT NOT NULL,
  "version" BIGINT NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_budget_accounts" PRIMARY KEY ("tenant_id", "account_key")
);

CREATE TABLE IF NOT EXISTS "budget_reservations" (
  "tenant_id" TEXT NOT NULL,
  "reservation_id" TEXT NOT NULL,
  "account_key" TEXT NOT NULL,
  "root_task_id" TEXT NOT NULL,
  "attempt_id" TEXT NOT NULL,
  "reserved" BIGINT NOT NULL,
  "actual" BIGINT,
  "status" TEXT NOT NULL,
  "period_key" TEXT NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  "updated_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_budget_reservations" PRIMARY KEY ("tenant_id", "reservation_id", "account_key"),
  FOREIGN KEY ("tenant_id", "account_key") REFERENCES "budget_accounts" ("tenant_id", "account_key")
);

CREATE TABLE IF NOT EXISTS "model_calls" (
  "tenant_id" TEXT NOT NULL,
  "call_id" TEXT NOT NULL,
  "run_id" TEXT NOT NULL,
  "step_id" TEXT NOT NULL,
  "logical_step_id" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "model_ref" TEXT NOT NULL,
  "prompt_version" TEXT NOT NULL,
  "input_digest" TEXT NOT NULL,
  "status" TEXT NOT NULL,
  "output" JSONB,
  "usage" JSONB,
  "reservation_id" TEXT,
  "provider_request_id" TEXT,
  "error" TEXT,
  "created_at" TIMESTAMPTZ NOT NULL,
  "finished_at" TIMESTAMPTZ,
  CONSTRAINT "pk_model_calls" PRIMARY KEY ("tenant_id", "call_id")
);

CREATE INDEX IF NOT EXISTS "ix_model_calls_0" ON "model_calls" ("tenant_id", "run_id", "logical_step_id");

CREATE TABLE IF NOT EXISTS "audit_entries" (
  "tenant_id" TEXT NOT NULL,
  "audit_id" TEXT NOT NULL,
  "seq" BIGINT NOT NULL,
  "at" TIMESTAMPTZ NOT NULL,
  "actor" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "task_id" TEXT,
  "root_task_id" TEXT,
  "subject_type" TEXT NOT NULL,
  "subject_id" TEXT NOT NULL,
  "from_state" TEXT,
  "to_state" TEXT,
  "reason" TEXT,
  "refs" JSONB NOT NULL,
  CONSTRAINT "pk_audit_entries" PRIMARY KEY ("tenant_id", "audit_id")
);

CREATE INDEX IF NOT EXISTS "ix_audit_entries_0" ON "audit_entries" ("tenant_id", "task_id", "seq");

CREATE TABLE IF NOT EXISTS "idempotency_records" (
  "tenant_id" TEXT NOT NULL,
  "principal" TEXT NOT NULL,
  "idem_key" TEXT NOT NULL,
  "request_digest" TEXT NOT NULL,
  "status_code" BIGINT NOT NULL,
  "response" JSONB NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_idempotency_records" PRIMARY KEY ("tenant_id", "principal", "idem_key")
);

CREATE TABLE IF NOT EXISTS "inbox_messages" (
  "tenant_id" TEXT NOT NULL,
  "message_id" TEXT NOT NULL,
  "effect_key" TEXT NOT NULL,
  "task_id" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "body" TEXT NOT NULL,
  "data" JSONB NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_inbox_messages" PRIMARY KEY ("tenant_id", "message_id"),
  CONSTRAINT "uq_inbox_messages_effect_key" UNIQUE ("tenant_id", "effect_key")
);

CREATE TABLE IF NOT EXISTS "quarantine" (
  "tenant_id" TEXT NOT NULL,
  "quarantine_id" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "ref" TEXT NOT NULL,
  "reason" TEXT NOT NULL,
  "payload" JSONB NOT NULL,
  "created_at" TIMESTAMPTZ NOT NULL,
  CONSTRAINT "pk_quarantine" PRIMARY KEY ("tenant_id", "quarantine_id")
);

CREATE TABLE IF NOT EXISTS "wk_counters" ("name" TEXT PRIMARY KEY, "value" BIGINT NOT NULL);

COMMIT;
