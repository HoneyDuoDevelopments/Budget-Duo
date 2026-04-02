-- Budget Duo V4.1.2 Migration
-- Adds a special 'n/a' category for transaction types with no category tree
-- Run: docker exec -i budget-duo-db psql -U budget_duo -d budget_duo < v4_1_2_migration.sql

BEGIN;

-- Insert n/a as a level-2 system category (child of no L1 — standalone sentinel)
INSERT INTO categories (id, name, parent_id, level, color, is_system, sort_order)
VALUES ('na', 'N/A', NULL, 2, '#3d5268', TRUE, 999)
ON CONFLICT (id) DO NOTHING;

-- Backfill: set cat_l2 = 'na' for transaction types that have no category tree
-- These types are intentionally uncategorizable: cc_payment, internal_transfer, ignore, savings_in, savings_out, investment_in, investment_out
UPDATE transactions
SET cat_l2 = 'na'
WHERE txn_class IN ('cc_payment','internal_transfer','ignore','savings_in','savings_out','investment_in','investment_out')
  AND cat_l2 IS NULL;

-- Verify
SELECT txn_class, cat_l2, COUNT(*) as cnt
FROM transactions
WHERE cat_l2 = 'na' OR txn_class IN ('cc_payment','internal_transfer','ignore','savings_in','savings_out','investment_in','investment_out')
GROUP BY txn_class, cat_l2
ORDER BY txn_class;

COMMIT;