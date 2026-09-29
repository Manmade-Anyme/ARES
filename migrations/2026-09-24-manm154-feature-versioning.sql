-- Migration: MANM-154 Feature Versioning and Missing Data Epoch Backfill
-- Adds feature_version to ml_collection and categorizes historical rows into schema epochs (v1 to v4).
-- Guarded with to_regclass for deployments where the optional ML module is disabled.

DO $$
BEGIN
  IF to_regclass('public.ml_collection') IS NOT NULL THEN
    ALTER TABLE ml_collection ADD COLUMN IF NOT EXISTS feature_version integer NOT NULL DEFAULT 4;
    CREATE INDEX IF NOT EXISTS idx_ml_collection_feature_version ON ml_collection (feature_version);

    -- Epoch 1: Initial inception to commit 782a240 (Dynamic SetupType enum, 2026-07-28T06:32:37Z)
    UPDATE ml_collection
    SET feature_version = 1
    WHERE timestamp < '2026-07-28T06:32:37Z';

    -- Epoch 2: Dynamic setup enum to commit ec2a84f (TASK-194 OI shape introduced, 2026-07-31T13:14:34Z)
    UPDATE ml_collection
    SET feature_version = 2
    WHERE timestamp >= '2026-07-28T06:32:37Z' AND timestamp < '2026-07-31T13:14:34Z';

    -- Epoch 3: OI shape & join integrity to commit a1b67ce (TASK-4c net_delta introduced, 2026-08-21T05:46:35Z)
    UPDATE ml_collection
    SET feature_version = 3
    WHERE timestamp >= '2026-07-31T13:14:34Z' AND timestamp < '2026-08-21T05:46:35Z';

    -- Epoch 4: Modern complete feature suite (TASK-4c onwards)
    UPDATE ml_collection
    SET feature_version = 4
    WHERE timestamp >= '2026-08-21T05:46:35Z';
  END IF;
END;
$$;

