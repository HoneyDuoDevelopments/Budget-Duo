-- ============================================================
-- Budget Duo — Transaction Audit Script
-- Run with:
-- docker compose exec db psql -U budget_duo -d budget_duo -f /audit.sql
-- ============================================================

\echo ''
\echo '===== 1. TRANSACTION TYPE BREAKDOWN ====='
\echo 'What Teller is sending us and how we are classifying it'
\echo ''
SELECT 
  txn_type,
  teller_category,
  is_income,
  COUNT(*) as count,
  SUM(ABS(amount)) as total_volume
FROM transactions
GROUP BY txn_type, teller_category, is_income
ORDER BY count DESC;

\echo ''
\echo '===== 2. ALL TRANSFERS — internal money moves ====='
\echo 'These should NOT count as income or spending'
\echo ''
SELECT 
  t.date,
  LEFT(t.description, 60) as description,
  t.counterparty_name,
  t.amount,
  t.txn_type,
  t.teller_category,
  t.is_income,
  a.name as account_name,
  a.owner
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE 
  t.txn_type = 'transfer'
  OR t.description ILIKE '%online banking transfer%'
  OR t.description ILIKE '%zelle%'
ORDER BY ABS(t.amount) DESC
LIMIT 40;

\echo ''
\echo '===== 3. LARGE DEPOSITS — what is coming in ====='
\echo 'Anything over $1000 credited to an account'
\echo ''
SELECT 
  t.date,
  LEFT(t.description, 60) as description,
  t.counterparty_name,
  t.amount,
  t.txn_type,
  t.teller_category,
  t.is_income,
  a.name as account_name,
  a.owner
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.amount > 1000
ORDER BY t.amount DESC
LIMIT 30;

\echo ''
\echo '===== 4. CREDIT CARD PAYMENTS from checking ====='
\echo 'These are double-counted — charges already on the card'
\echo ''
SELECT 
  t.date,
  LEFT(t.description, 60) as description,
  t.amount,
  t.txn_type,
  a.name as account_name,
  a.owner
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE 
  t.txn_type = 'payment'
  OR t.description ILIKE '%payment%'
  OR t.description ILIKE '%autopay%'
ORDER BY t.date DESC
LIMIT 30;

\echo ''
\echo '===== 5. ACH TRANSACTIONS — payroll, bills, misc ====='
\echo ''
SELECT 
  t.date,
  LEFT(t.description, 70) as description,
  t.counterparty_name,
  t.amount,
  t.teller_category,
  t.is_income,
  a.name as account_name,
  a.owner
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.txn_type = 'ach'
ORDER BY t.amount DESC
LIMIT 40;

\echo ''
\echo '===== 6. MONTHLY SUMMARY — raw numbers ====='
\echo 'Total in vs out per month including transfers'
\echo ''
SELECT
  TO_CHAR(DATE_TRUNC('month', date), 'Mon YYYY') as month,
  SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END)::numeric(12,2) as total_in,
  SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END)::numeric(12,2) as total_out,
  SUM(CASE WHEN txn_type = 'transfer' AND amount > 0 THEN amount ELSE 0 END)::numeric(12,2) as transfer_in,
  SUM(CASE WHEN txn_type = 'transfer' AND amount < 0 THEN ABS(amount) ELSE 0 END)::numeric(12,2) as transfer_out,
  SUM(CASE WHEN txn_type = 'payment' THEN ABS(amount) ELSE 0 END)::numeric(12,2) as cc_payments,
  COUNT(*) as txn_count
FROM transactions
GROUP BY DATE_TRUNC('month', date)
ORDER BY DATE_TRUNC('month', date) DESC
LIMIT 8;

\echo ''
\echo '===== 7. MONTHLY SUMMARY — transfers excluded ====='
\echo 'What the real income vs spending looks like without noise'
\echo ''
SELECT
  TO_CHAR(DATE_TRUNC('month', date), 'Mon YYYY') as month,
  SUM(CASE WHEN is_income = true THEN amount ELSE 0 END)::numeric(12,2) as real_income,
  SUM(CASE WHEN is_income = false 
       AND txn_type NOT IN ('transfer', 'payment') 
       AND amount < 0 
       THEN ABS(amount) ELSE 0 END)::numeric(12,2) as real_spending,
  COUNT(CASE WHEN txn_type = 'transfer' THEN 1 END) as transfer_count,
  COUNT(CASE WHEN txn_type = 'payment' THEN 1 END) as payment_count
FROM transactions
GROUP BY DATE_TRUNC('month', date)
ORDER BY DATE_TRUNC('month', date) DESC
LIMIT 8;

\echo ''
\echo '===== 8. RECURRING DETECTION — what we are flagging ====='
\echo ''
SELECT 
  recurring_group,
  COUNT(*) as occurrences,
  MIN(ABS(amount))::numeric(10,2) as min_amt,
  MAX(ABS(amount))::numeric(10,2) as max_amt,
  AVG(ABS(amount))::numeric(10,2) as avg_amt,
  STDDEV(ABS(amount))::numeric(10,2) as stddev_amt,
  MIN(date) as first_seen,
  MAX(date) as last_seen
FROM transactions
WHERE is_recurring = true
GROUP BY recurring_group
ORDER BY occurrences DESC;

\echo ''
\echo '===== 9. POTENTIAL RECURRING — missed by detector ====='
\echo 'Merchants appearing 2+ times not currently flagged'
\echo ''
SELECT 
  COALESCE(counterparty_name, LEFT(description, 40)) as merchant,
  COUNT(*) as occurrences,
  AVG(ABS(amount))::numeric(10,2) as avg_amt,
  STDDEV(ABS(amount))::numeric(10,2) as stddev_amt,
  MIN(date) as first_seen,
  MAX(date) as last_seen,
  COUNT(CASE WHEN is_recurring THEN 1 END) as already_flagged
FROM transactions
WHERE is_income = false
  AND txn_type NOT IN ('transfer', 'payment')
  AND status = 'posted'
GROUP BY COALESCE(counterparty_name, LEFT(description, 40))
HAVING COUNT(*) >= 2
ORDER BY occurrences DESC, avg_amt DESC
LIMIT 50;

\echo ''
\echo '===== 10. UNCATEGORIZED CARD PAYMENTS by merchant ====='
\echo 'High frequency merchants with no Teller category'
\echo ''
SELECT 
  COALESCE(t.counterparty_name, LEFT(t.description, 50)) as merchant,
  COUNT(*) as txn_count,
  SUM(ABS(t.amount))::numeric(12,2) as total_spent,
  AVG(ABS(t.amount))::numeric(10,2) as avg_amt,
  a.owner
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.txn_type = 'card_payment'
  AND t.teller_category IS NULL
  AND t.is_income = false
GROUP BY COALESCE(t.counterparty_name, LEFT(t.description, 50)), a.owner
ORDER BY txn_count DESC
LIMIT 40;

\echo ''
\echo '===== AUDIT COMPLETE ====='