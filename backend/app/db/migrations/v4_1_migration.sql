-- Budget Duo V4.1 Migration
-- Adds cat_l5 for dynamic entry (trip/car/project) at the 5th category level
-- Run: docker exec -i budget-duo-db psql -U budget_duo -d budget_duo < v4_1_migration.sql

BEGIN;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cat_l5 TEXT REFERENCES dynamic_entries(id) ON DELETE SET NULL;

-- Verify
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'transactions' AND column_name LIKE 'cat_l%'
ORDER BY column_name;

COMMIT;