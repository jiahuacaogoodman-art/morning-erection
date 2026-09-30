-- V0.3 P0: actions bind their resource scope and data egress into payload_digest.
-- 0001_init.sql (regenerated from the catalogue) already contains these columns; this file
-- upgrades a database created from the v0.2 schema. Rows written by v0.2 had no scope/egress
-- in their digest, so their digests will not recompute: dispatch refuses them with
-- `digest_mismatch` (fail closed). Drain or resolve v0.2 in-flight actions before upgrading.
ALTER TABLE "actions" ADD COLUMN IF NOT EXISTS "resource_scope" JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE "actions" ADD COLUMN IF NOT EXISTS "data_egress" JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE "actions" ALTER COLUMN "resource_scope" DROP DEFAULT;
ALTER TABLE "actions" ALTER COLUMN "data_egress" DROP DEFAULT;
