-- ============================================================
-- Budget Duo V3 Migration
-- Run with:
-- docker compose exec -T db psql -U budget_duo -d budget_duo < migrate_v3.sql
-- ============================================================

BEGIN;

-- ============================================================
-- 1. FIX sync_log columns: String -> Integer
-- ============================================================
ALTER TABLE sync_log
  ALTER COLUMN txns_added TYPE INTEGER USING COALESCE(txns_added::integer, 0),
  ALTER COLUMN txns_updated TYPE INTEGER USING COALESCE(txns_updated::integer, 0);

ALTER TABLE sync_log
  ALTER COLUMN txns_added SET DEFAULT 0,
  ALTER COLUMN txns_updated SET DEFAULT 0;

-- ============================================================
-- 2. Ensure all user-verified transactions are properly protected
-- Any transaction with a manually set category should be verified
-- ============================================================
UPDATE transactions
SET user_verified = TRUE
WHERE category_id IS NOT NULL
  AND user_verified = FALSE
  AND rule_id IS NULL;

-- ============================================================
-- 3. Fix ACH misclassification
-- Previously all negative ACH was set to cc_payment
-- Reclassify non-verified ACH debits that are clearly not CC payments
-- ============================================================
UPDATE transactions
SET txn_class = 'expense'
WHERE txn_class = 'cc_payment'
  AND txn_type = 'ach'
  AND user_verified = FALSE
  AND description NOT ILIKE '%capital one%'
  AND description NOT ILIKE '%citi card%'
  AND description NOT ILIKE '%american express%'
  AND description NOT ILIKE '%applecard%'
  AND description NOT ILIKE '%amazon corp%'
  AND description NOT ILIKE '%home depot des%'
  AND description NOT ILIKE '%payment to crd%';

-- ============================================================
-- 4. Sync is_income flag with txn_class
-- Fix stale is_income values from the V2 classifier bug
-- ============================================================
UPDATE transactions
SET is_income = TRUE
WHERE txn_class = 'income' AND is_income = FALSE;

UPDATE transactions
SET is_income = FALSE
WHERE txn_class IN ('expense', 'internal_transfer', 'cc_payment',
                     'savings_move', 'investment_in', 'investment_out',
                     'debt_payment', 'ignore')
  AND is_income = TRUE;

COMMIT;

\echo ''
\echo '✅ V3 Migration complete'
\echo ''
\echo 'Changes applied:'
\echo '  - sync_log txns_added/updated converted to INTEGER'
\echo '  - User-verified flag backfilled for manual category assignments'
\echo '  - ACH misclassification corrected'
\echo '  - is_income flag synced with txn_class'
\echo ''
\echo 'Next: Re-run classifier to apply updated rules'
\echo 'docker compose exec backend python -m app.services.classify_backfill'
\echo ''